import re

import click
import json

from junit_xml import to_xml_report_string
from dbt_junitxml.dbt_junit_xml import DBTTestSuite, DBTTestCase
from datetime import datetime
import os
from pathlib import Path
import logging


class InvalidRunResult(Exception):
    pass


def convert_timestamp_to_isoformat(timestamp: str) -> str:
    return datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%S.%fZ').strftime(
        '%Y-%m-%dT%H:%M:%S')


def get_custom_properties(path: str, custom_properties: dict) -> dict:
    """
    We receive path "models/anywhere/hiring/ and
    custom_attributes = [{"Area":"path_levels[1]", "Source":"path_levels[2]"}]
    We return {"attribute": ["Area:anywhere","Source:hiring"]}
    """
    path_levels = Path(path).parts
    properties = {"attribute": []}
    for key, value in custom_properties.items():
        try:
            if re.match(r"path_levels\[\d+]", value):
                index = int(re.search(r"\d+", value).group())
                attribute_value = f"{key}:{path_levels[index]}"
            else:
                attribute_value = f"{key}:{value}"
            properties["attribute"].append(attribute_value)

        except IndexError:
            logging.error(f"Index out of range for {key}: {value}, path: {path}.")
            continue

        except Exception as e:
            logging.error(f"Error updating custom property '{key}': {e}")
            continue

    return properties


def validate_path(path: str) -> str:
    if not Path(path).exists():
        raise ValueError(f"Invalid path: {path}")
    return path


def validate_custom_properties(ctx, param, value):
    if value is None:
        return None

    properties = {}
    if not isinstance(value, (tuple, list)):
        value = [value]

    for prop_group in value:
        items = prop_group.split(',')
        for item in items:
            item = item.strip()
            if '=' not in item:
                raise click.BadParameter(
                    f"Invalid custom property '{item}'. "
                    "Properties must be in the format key=value."
                )
            key, val = item.split('=', 1)
            key = key.strip()
            val = val.strip()
            if not key or not val:
                raise click.BadParameter(
                    f"Invalid custom property '{item}'. "
                    "Both key and value must be non-empty."
                )
            if key in properties:
                raise click.BadParameter(
                    f"Duplicate custom property key '{key}'. "
                    "Each key must be unique."
                )
            properties[key] = val
    return properties


@click.group()
def cli():
    pass


@cli.command()
@click.option('--manifest', '-m', type=click.Path(exists=True),
              default=Path('target/manifest.json'),
              help='DBT manifest file name')
@click.option('--run_result', '-r', type=click.Path(exists=True),
              default=Path('target/run_results.json'),
              help='DBT run results file name')
@click.option('--output', '-o', type=click.Path(exists=False),
              default='report.xml',
              help='Report output file name',)
@click.option('--custom_properties', '-cp', multiple=True,
              type=str, help='Add custom properties to the report, '
                             'e.g. --custom_properties key1=value1 --custom_properties key2=value2',
              prompt_required=False, callback=validate_custom_properties, default=None)
def parse(run_result, manifest, output, custom_properties=None):
    with open(run_result) as f:
        run_result = json.load(f)

    with open(manifest) as m:
        manifest = json.load(m)['nodes']

    try:
        executed_command = run_result["args"]["which"] if 'which' in run_result["args"].keys() else run_result["args"][
            "rpc_method"]
        schema_version = run_result["metadata"]["dbt_schema_version"]

        if schema_version not in [
            "https://schemas.getdbt.com/dbt/run-results/v4.json",
            "https://schemas.getdbt.com/dbt/run-results/v5.json",
            "https://schemas.getdbt.com/dbt/run-results/v6.json",
        ]:
            raise InvalidRunResult("run_result.json other than (v4-v6) are not supported.")

        if not executed_command == "test":
            raise InvalidRunResult(
                f"run_result.json must be from the output of `dbt test`. Got dbt {executed_command}.")

    except KeyError as e:
        raise InvalidRunResult(e)

    tests = run_result["results"]
    total_elapsed_time = run_result["elapsed_time"]
    test_suite_timestamp = convert_timestamp_to_isoformat(run_result["metadata"]["generated_at"])

    tests_manifest = {}
    for key, config in manifest.items():
        if config['resource_type'] == 'test':
            test_name = key.split('.')[2]
            tests_manifest[test_name] = config
            sql_log = \
                f"""select * from {tests_manifest[test_name]['schema']}.{tests_manifest[test_name]['alias']
                if tests_manifest[test_name]['alias'] else tests_manifest[test_name]['name']}"""
            sql_log_format = "\n" + '-' * 96 + "\n" + sql_log + "\n" + '-' * 96
            if 'compiled_sql' in config.keys():
                sql_text = config['compiled_sql']
            elif 'compiled_code' in config.keys():
                sql_text = config['compiled_code']
            elif 'raw_code' in config.keys():
                sql_text = config['raw_code']
            else:
                sql_text = config['raw_sql']
            sql_text = [sql_log_format, sql_text]
            tests_manifest[test_name]['sql'] = str.join('', sql_text)
            tests_manifest[test_name]['properties'] = get_custom_properties(config['original_file_path'],
                                                                            custom_properties)

    test_cases = []
    for test in tests:
        test_name = test["unique_id"].split('.')[2]
        test_timestamp = test['timing'][0]["started_at"] if ["status"] == 'pass' \
            else test_suite_timestamp
        test_sql = tests_manifest[test_name]["sql"] if test_name in tests_manifest.keys() else 'N/A'
        test_case = DBTTestCase(
            classname=test["unique_id"],
            name=test["unique_id"].split(".")[2],
            elapsed_sec=test["execution_time"],
            status=test["status"],
            timestamp=test_timestamp,
            stdout=test_sql,
            properties=tests_manifest[test_name]["properties"] if test_name in tests_manifest.keys() else None
        )

        if test["status"] == "fail":
            test_case.add_failure_info(message=test["message"], output=test["message"])

        if test["status"] == "error":
            test_case.add_error_info(message=test["message"], output=test["message"])

        if test["status"] == "skipped":
            test_case.add_skipped_info(message=test["message"], output=test["message"])

        test_cases.append(test_case)

    test_suite = DBTTestSuite(f"Tests",
                              test_cases=test_cases,
                              time=total_elapsed_time,
                              timestamp=test_suite_timestamp)

    xml_report = to_xml_report_string([test_suite])

    with open(output, mode="wb") as o:
        o.write(xml_report.encode())
