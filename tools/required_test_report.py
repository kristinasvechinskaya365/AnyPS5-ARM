import argparse
import json
import os
import platform
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def inventory(path, kind):
    if kind == "ctest":
        data = json.loads(path.read_text(encoding="utf-8"))
        names = [test["name"] for test in data["tests"]]
    else:
        names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                 if re.fullmatch(r"[^\s/]+\.[^\s/]+/[^\s]+", line.strip())]
    if any(not isinstance(name, str) or not name for name in names) or len(names) != len(set(names)):
        raise ValueError("test inventory contains empty or duplicate names")
    if not names:
        raise ValueError("test inventory is empty")
    return sorted(names)


def results(path, kind):
    root = ET.parse(path).getroot()
    if root.tag not in ("testsuite", "testsuites"):
        raise ValueError(f"expected JUnit testsuite or testsuites, got {root.tag!r}")
    for suite in root.iter():
        if suite.tag not in ("testsuite", "testsuites"):
            continue
        for child in suite:
            if child.tag in ("error", "failure", "skipped"):
                raise ValueError(f"JUnit {suite.tag} contains a suite-level {child.tag}")
        cases = list(suite.iter("testcase"))
        counts = {"tests": len(cases),
                  "failures": sum(case.find("failure") is not None for case in cases),
                  "errors": sum(case.find("error") is not None for case in cases),
                  "skipped": sum(case.find("skipped") is not None for case in cases),
                  "disabled": sum(case.get("status") == "disabled" for case in cases)}
        for name, count in counts.items():
            declared = suite.get(name)
            if declared is not None and (re.fullmatch(r"[0-9]+", declared) is None or int(declared) != count):
                raise ValueError(f"JUnit {suite.tag} {name} count {declared!r} differs from {count} testcase records")
    found = {}
    for case in root.iter("testcase"):
        name = case.get("name", "")
        if kind == "swift":
            classname = case.get("classname", "")
            if not classname:
                raise ValueError("Swift testcase has no classname")
            name = f"{classname}/{name}" if name else ""
        if not name or name in found:
            raise ValueError(f"empty or duplicate testcase name: {name!r}")
        if sum(case.find(tag) is not None for tag in ("failure", "error", "skipped")) > 1:
            raise ValueError(f"JUnit testcase has conflicting outcomes: {name}")
        if case.find("failure") is not None or case.find("error") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "skipped"
        elif case.get("status") not in (None, "run"):
            status = "not_run"
        else:
            status = "passed"
        found[name] = {"status": status, "seconds": case.get("time")}
    return found


def swift_outcomes(path, expected=()):
    found = {}
    pattern = re.compile(r"Test Case '(?:-\[([^\s\]]+) ([^\]]+)\]|([^']+))' (passed|failed|skipped) \(")
    for match in pattern.finditer(path.read_text(encoding="utf-8")):
        classname, method, portable, status = match.groups()
        if portable:
            classname, _, method = portable.rpartition(".")
        if not classname or not method:
            raise ValueError(f"unrecognized XCTest completion: {match.group(0)}")
        name = f"{classname}/{method}"
        if expected and name not in expected and "." not in classname:
            matches = [candidate for candidate in expected if candidate.endswith("." + name)]
            if len(matches) != 1:
                raise ValueError(f"XCTest completion does not identify one discovered test: {name}")
            name = matches[0]
        if name in found:
            raise ValueError(f"duplicate XCTest completion: {name}")
        found[name] = status
    return found


def known_failures(path, names):
    found = []
    if path is None:
        return found
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split(maxsplit=2)
        if len(fields) != 3:
            raise ValueError(f"{path}:{number}: expected platforms, pattern and reason")
        hosts, pattern, reason = fields
        if hosts != "all" and "macos" not in hosts.split(","):
            continue
        matching = [name for name in names if re.fullmatch(pattern, name)]
        found.append({"pattern": pattern, "reason": reason, "registered_tests": matching})
    return found


def report(inventory_path, junit_path, kind, required, failures_path=None, log_path=None):
    errors = []
    names = []
    cases = {}
    failures = []
    try:
        names = inventory(inventory_path, kind)
    except (OSError, ValueError, KeyError, TypeError) as error:
        errors.append(f"inventory: {error}")
    selected = set()
    for pattern in required:
        matches = {name for name in names if re.fullmatch(pattern, name)}
        if not matches:
            errors.append(f"required pattern has no registered tests: {pattern}")
        selected.update(matches)
    try:
        cases = results(junit_path, kind)
    except (OSError, ValueError, ET.ParseError) as error:
        errors.append(f"results: {error}")
    if kind == "swift":
        outcomes = {}
        try:
            if log_path is None:
                raise ValueError("verbose XCTest log is required")
            outcomes = swift_outcomes(log_path, names)
        except (OSError, ValueError) as error:
            errors.append(f"XCTest output: {error}")
        for name, case in cases.items():
            if name not in outcomes:
                errors.append(f"XCTest completion is missing: {name}")
                if case["status"] == "passed":
                    case["status"] = "not_run"
            elif case["status"] != "passed" and outcomes[name] != case["status"]:
                errors.append(f"JUnit and XCTest statuses disagree for {name}: {case['status']} versus {outcomes[name]}")
            elif outcomes[name] != "passed":
                case["status"] = outcomes[name]
    try:
        failures = known_failures(failures_path, names)
    except (OSError, ValueError, re.error) as error:
        errors.append(f"known failures: {error}")
    for entry in failures:
        overlap = selected.intersection(entry["registered_tests"])
        if overlap:
            errors.append(f"required tests are still excluded in broad CI: {', '.join(sorted(overlap))}")
    missing = sorted(selected.difference(cases))
    unexpected = sorted(set(cases).difference(selected))
    if missing:
        errors.append(f"required tests did not report results: {', '.join(missing)}")
    if unexpected:
        errors.append(f"unexpected results outside required selection: {', '.join(unexpected)}")
    for name in sorted(selected.intersection(cases)):
        if cases[name]["status"] != "passed":
            errors.append(f"required test {name}: {cases[name]['status']}")
    counts = Counter(cases[name]["status"] for name in selected.intersection(cases))
    return {
        "format_version": 1,
        "kind": kind,
        "registered": len(names),
        "required": len(selected),
        "passed": counts["passed"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "not_run": counts["not_run"],
        "missing": missing,
        "unexpected": unexpected,
        "tests": {name: cases.get(name, {"status": "missing"}) for name in sorted(selected)},
        "known_macos_failures": failures,
        "not_selected": sorted(set(names).difference(selected)),
        "errors": errors,
    }


def markdown(data):
    lines = [f"### Required {data['kind']} test results", "",
             f"Revision: `{data['revision']}`", "",
             f"Required: {data['required']}; passed: {data['passed']}; failed: {data['failed']}; "
             f"skipped: {data['skipped']}; not run: {data['not_run']}; missing: {len(data['missing'])}.", "",
             "Hosted macOS validation covers the selected synthetic tests. GPU rendering, audio, "
             "commercial gameplay and physical M2 performance are not exercised."]
    if data["errors"]:
        lines += ["", "Errors:", ""] + [f"- {error}" for error in data["errors"]]
    if data["known_macos_failures"]:
        lines += ["", "Known failures excluded by the existing broad macOS suite remain unverified:", ""]
        for entry in data["known_macos_failures"]:
            lines.append(f"- `{entry['pattern']}`: {entry['reason']}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Require every selected registered test to report a pass")
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--junit", required=True, type=Path)
    parser.add_argument("--kind", choices=("ctest", "swift"), default="ctest")
    parser.add_argument("--require", required=True, action="append")
    parser.add_argument("--known-failures", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    for pattern in args.require:
        try:
            re.compile(pattern)
        except re.error as error:
            parser.error(f"invalid required pattern {pattern!r}: {error}")
    data = report(args.inventory, args.junit, args.kind, args.require, args.known_failures, args.log)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    data.update(revision=revision, host=platform.platform(), machine=platform.machine(),
                event_revision=os.environ.get("GITHUB_SHA"), run_id=os.environ.get("GITHUB_RUN_ID"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    summary = markdown(data)
    args.output.with_suffix(".md").write_text(summary, encoding="utf-8")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(summary)
    print(summary)
    return bool(data["errors"])


if __name__ == "__main__":
    sys.exit(main())
