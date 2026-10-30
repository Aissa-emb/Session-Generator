#!/usr/bin/env python3
"""Regenerate the Pydantic contract deterministically; --check fails on drift."""
import argparse
from pathlib import Path
import subprocess  # nosec B404 -- fixed local schema compiler
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--check', action='store_true')
a = p.parse_args()
with tempfile.TemporaryDirectory() as d:
    output = Path(d) / 'models.py'
    subprocess.run([  # nosec B603 -- fixed executable/module, local schema only
sys.executable, '-m', 'datamodel_code_generator', '--input', str(root / 'spec/scenario.schema.json'),
                    '--input-file-type', 'jsonschema', '--output', str(output), '--output-model-type', 'pydantic_v2.BaseModel',
                    '--target-python-version', '3.12', '--disable-timestamp', '--class-name', 'ScenarioSpec',
                    '--use-standard-collections', '--use-union-operator', '--no-allow-remote-refs', '--formatters', 'black', 'isort'], check=True)
    target = root / 'server/scenario_models.py'
    if a.check:
        if target.read_bytes() != output.read_bytes():
            sys.exit('Generated Pydantic models differ; run tools/generate_models.py.')
    else:
        target.write_bytes(output.read_bytes())
