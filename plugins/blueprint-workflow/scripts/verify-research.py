"""Explicit, synthetic native research acceptance. Uses the existing CLI login.

Writes only to the supplied private data directory. No host installation or
existing task is changed. A passing result enables this exact CLI binary there.
"""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from blueprint_workflow.paths import private_data_dir
from blueprint_workflow.research import (CodexResearchProvider, _decode_research,
    mark_research_verified, research_capabilities)
from blueprint_workflow.analyzer import AnalysisProtocolError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--model')
    args = parser.parse_args()
    directory = private_data_dir(args.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    provider = CodexResearchProvider(directory, model=args.model, allow_unverified=True)
    request = {'id': 'synthetic-native-research-acceptance',
        'question': 'Open https://docs.python.org/3/library/json.html and compare json.load with json.loads, including actual supported inputs and JSONDecodeError. Use and cite ONLY that exact page after explicitly opening it; do not cite search snippets or any unopened URL. This is an isolated capability test, not a rule for any workflow.',
        'reason': 'Verify real public-page research independently from workflow design.',
        'evidence_event_ids': ['synthetic-acceptance-source'], 'official_domains': ['docs.python.org']}
    command = provider._command(directory, directory / 'schema-probe.json', directory / 'output-probe.json', request['official_domains'])
    assert 'read-only' in command and '--ignore-user-config' in command
    for feature in ('shell_tool', 'apps', 'plugins', 'hooks', 'multi_agent'):
        assert any(command[i:i+2] == ['--disable', feature] for i in range(len(command)-1))
    try:
        _decode_research(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'forbidden'}}), directory / 'unused.json', request)
    except AnalysisProtocolError:
        pass
    else:
        raise AssertionError('Non-web tool output was accepted')
    print(json.dumps({'phase': 'contract_checks_passed', 'real_model_request': 'starting'}), flush=True)
    result = provider.research({'request': request})
    if result['status'] != 'succeeded':
        raise RuntimeError('Live research did not succeed: ' + result['summary'])
    report = {'test': 'explicit synthetic native research', 'result': result,
              'negative_checks': ['read-only contract', 'effectful features disabled', 'non-web trace rejected'],
              'limits': ['Runtime tool restrictions and result trace checks; not a proof about every future CLI release.']}
    report_path = directory / 'native-research-acceptance.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    mark_research_verified(directory, negative_evidence=str(report_path) + '#negative_checks', live_evidence=str(report_path) + '#result')
    print(json.dumps({'phase': 'passed', 'report': str(report_path), 'capabilities': research_capabilities(directory)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
