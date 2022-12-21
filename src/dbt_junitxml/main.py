import click
import json

from junit_xml import to_xml_report_string
from dbt_junitxml.dbt_junit_xml import DBTTestSuite, DBTTestCase
from datetime import datetime
import os


class InvalidRunResult(Exception):
    pass


def convert_timestamp_to_isoformat(timestamp: str) -> str:
    return datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%S.%fZ').strftime(
        '%Y-%m-%dT%H:%M:%S')


def convert_seconds_to_duration(seconds):
    hour = int(seconds // 3600)
    minute = int((seconds - hour * 3600) // 60)
    second = int(seconds - hour * 3600 - minute * 60)
    return f'{hour}h {minute}m {second}s'


@click.group()
def cli():
    pass


@cli.command()
@click.argument(
    "run_result",
    type=click.Path(exists=True)
)
@click.argument(
    "manifest",
    type=click.Path(exists=True)
)
@click.argument(
    "output",
    type=click.Path(exists=False)
)
def parse(run_result, manifest, output):
    with open(run_result) as f:
        run_result = json.load(f)

    with open(manifest) as m:
        manifest = json.load(m)['nodes']

    try:
        rpc_method = run_result["args"]["rpc_method"]
        schema_version = run_result["metadata"]["dbt_schema_version"]

        if not schema_version == "https://schemas.getdbt.com/dbt/run-results/v4.json":
            raise InvalidRunResult("run_result.json other than v4 are not supported.")

        if not rpc_method == "test":
            raise InvalidRunResult(
                f"run_result.json must be from the output of `dbt test`. Got dbt {rpc_method}.")

    except KeyError as e:
        raise InvalidRunResult(e)

    tests = run_result["results"]
    total_elapsed_time = run_result["elapsed_time"]
    test_suite_timestamp = convert_timestamp_to_isoformat(run_result["metadata"]["generated_at"])

    unified_nodes = {}
    for key, value in manifest.items():
        test_name = key.split('.')[2].replace('a_type_critical_', '')
        unified_nodes[test_name] = value
        sql_path = os.path.join(value['root_path'], 'target', 'compiled', 'anywhere_analytics',
                                value['original_file_path'], value['path'])
        try:
            with open(sql_path, 'r') as sql:
                unified_nodes[test_name]['sql'] = str.join('', sql.readlines())
        except FileNotFoundError as e:
            unified_nodes[test_name]['sql'] = value['raw_sql']

    test_cases = []
    for test in tests:
        test_name = test["unique_id"].split('.')[2].replace('a_type_critical_', '')
        test_timestamp = test['timing'][0]["started_at"] if ["status"] == 'pass' \
            else test_suite_timestamp
        test_sql = unified_nodes[test_name]["sql"] if test_name in unified_nodes.keys() else 'N/A'
        test_case = DBTTestCase(
            classname=test["unique_id"],
            name=test["unique_id"].split(".")[2],
            elapsed_sec=test["execution_time"],
            status=test["status"],
            timestamp=test_timestamp,
            stdout=test_sql
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
