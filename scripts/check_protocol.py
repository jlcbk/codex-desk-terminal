#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0.4-draft 协议 fixtures 校验器（DRAFT，待 A0 冻结）。

运行（系统 python3.9 无 jsonschema，必须用 uv 注入）：

    /Users/cui/.local/bin/uv run --with jsonschema python scripts/check_protocol.py

检查分两层，对应设备有界解析（P1.4）必须执行的完整规则集：

  A. JSON Schema（draft-2020-12）：protocol/state.schema.json。
  B. 解析器级检查（JSON Schema 无法表达或明确留给解析器的规则，真源
     docs/INTERFACES.md §3）：
       B1 utf8        合法 UTF-8 严格解码（§8 协议行「非法 UTF-8」）
       B2 size        整包 <=16384 字节（§3 完整消息行）
       B3 depth       嵌套深度 <=12，根对象计 1、容器每层 +1（§3 完整消息行）
       B4 bytes       cdt-utf8-max-bytes 注解字段的 UTF-8 字节上限
                      （§3 字符串行；schema maxLength 按字符计只是近似上界）
       B5 consistency threads_total >= len(threads)、usage.windows_total >=
                      len(windows)、thread id 唯一、selected_thread_id 为 null
                      或指向已包含线程（§3 threads/usage/selected_thread_id 行）

预期结果由文件名前缀决定：valid_* 必须通过全部检查；invalid_* 必须至少被
一层拒绝。全部 PASS 退出码 0，任一 FAIL 退出码 1。

telemetry.schema.json 目前无 fixtures（INTERFACES §1 未定义字段，草案待 A0
冻结），仅做 schema 自检。
"""
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "protocol"
STATE_SCHEMA_PATH = REPO / "protocol" / "state.schema.json"
TELEMETRY_SCHEMA_PATH = REPO / "protocol" / "telemetry.schema.json"

MAX_TOTAL_BYTES = 16384  # §3 完整消息行
MAX_DEPTH = 12           # §3 完整消息行（根对象计 1）


def collect_byte_limits(node, path=()):
    """从 schema 收集 cdt-utf8-max-bytes 注解 -> (实例路径, 字节上限) 列表。"""
    found = []
    if isinstance(node, dict):
        cap = node.get("cdt-utf8-max-bytes")
        if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
            found.append((path, cap))
        for key, sub in node.get("properties", {}).items():
            found.extend(collect_byte_limits(sub, path + (key,)))
        if isinstance(node.get("items"), dict):
            found.extend(collect_byte_limits(node["items"], path + ("[]",)))
        for comb in ("oneOf", "anyOf", "allOf"):
            for sub in node.get(comb) or []:
                found.extend(collect_byte_limits(sub, path))
    return found


def iter_at(doc, path):
    """按 schema 路径（元组，"[]" 表示数组逐元素）产出实例中的值。"""
    if not path:
        yield doc
        return
    head, rest = path[0], path[1:]
    if head == "[]":
        if isinstance(doc, list):
            for item in doc:
                for v in iter_at(item, rest):
                    yield v
    elif isinstance(doc, dict):
        for key, value in doc.items():
            if key == head:
                for v in iter_at(value, rest):
                    yield v


def byte_limit_errors(doc, limits):
    """B4：注解字段的 UTF-8 字节数检查（只作用于字符串值）。"""
    errors = []
    for path, cap in limits:
        for value in iter_at(doc, path):
            if isinstance(value, str):
                size = len(value.encode("utf-8"))
                if size > cap:
                    where = "/".join(str(p) for p in path) or "<root>"
                    errors.append("%s: %d B > %d B" % (where, size, cap))
    return errors


def json_depth(node):
    """B3：根对象计 1，容器（object/array）每层 +1，标量计 0。"""
    if isinstance(node, dict):
        return 1 + max((json_depth(v) for v in node.values()), default=0)
    if isinstance(node, list):
        return 1 + max((json_depth(v) for v in node), default=0)
    return 0


def consistency_errors(doc):
    """B5：跨字段一致性（§3 threads/usage/selected_thread_id 行）。"""
    errors = []
    if not isinstance(doc, dict):
        return errors
    threads = doc.get("threads")
    if isinstance(threads, list):
        total = doc.get("threads_total")
        if isinstance(total, int) and not isinstance(total, bool):
            if total < len(threads):
                errors.append("threads_total=%r < len(threads)=%d" % (total, len(threads)))
        ids = [t.get("id") for t in threads if isinstance(t, dict)]
        str_ids = [i for i in ids if isinstance(i, str)]
        if len(str_ids) != len(set(str_ids)):
            errors.append("thread id 不唯一")
        selected = doc.get("selected_thread_id")
        if selected is not None and selected not in str_ids:
            errors.append("selected_thread_id=%r 不指向已包含线程" % (selected,))
    usage = doc.get("usage")
    if isinstance(usage, dict):
        windows = usage.get("windows")
        total = usage.get("windows_total")
        if isinstance(windows, list) and isinstance(total, int) and not isinstance(total, bool):
            if total < len(windows):
                errors.append("usage.windows_total=%r < len(windows)=%d" % (total, len(windows)))
    return errors


def schema_errors_text(validator, doc):
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    out = []
    for err in errors[:5]:
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        msg = err.message
        if len(msg) > 120:
            msg = msg[:117] + "..."
        out.append("%s: %s" % (where, msg))
    if len(errors) > 5:
        out.append("... 共 %d 处" % len(errors))
    return "; ".join(out)


def check_file(path, validator, limits):
    """返回 (accepted, checks)；checks 为 [(层名, 是否通过, 说明), ...]。"""
    raw = path.read_bytes()
    checks = []

    # B1 合法 UTF-8
    text = None
    try:
        text = raw.decode("utf-8", errors="strict")
        checks.append(("utf8", True, ""))
    except UnicodeDecodeError as exc:
        checks.append(("utf8", False, "非法 UTF-8: %s" % exc))

    doc = None
    if text is not None:
        try:
            doc = json.loads(text)
            checks.append(("json", True, ""))
        except json.JSONDecodeError as exc:
            checks.append(("json", False, "JSON 解析失败: %s" % exc))

    # B2 整包字节
    size = len(raw)
    checks.append(("size", size <= MAX_TOTAL_BYTES, "%d B (上限 %d)" % (size, MAX_TOTAL_BYTES)))

    if doc is not None:
        # A schema
        msg = schema_errors_text(validator, doc)
        checks.append(("schema", not msg, msg))
        # B4 注解字节上限
        errs = byte_limit_errors(doc, limits)
        checks.append(("bytes", not errs, "; ".join(errs[:5])))
        # B3 深度
        depth = json_depth(doc)
        checks.append(("depth", depth <= MAX_DEPTH, "深度 %d (上限 %d)" % (depth, MAX_DEPTH)))
        # B5 一致性
        errs = consistency_errors(doc)
        checks.append(("consistency", not errs, "; ".join(errs)))

    accepted = all(ok for _, ok, _ in checks)
    return accepted, checks


def main():
    failures = 0

    for schema_path in (STATE_SCHEMA_PATH, TELEMETRY_SCHEMA_PATH):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        try:
            Draft202012Validator.check_schema(schema)
            print("[OK]   schema 自检 %s" % schema_path.name)
        except Exception as exc:  # noqa: BLE001 - 报告任何 schema 结构错误
            print("[FAIL] schema 自检 %s: %s" % (schema_path.name, exc))
            failures += 1

    state_schema = json.loads(STATE_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(state_schema)
    limits = collect_byte_limits(state_schema)
    print("[OK]   cdt-utf8-max-bytes 注解 %d 处" % len(limits))
    print()

    files = sorted(list(FIXTURES.glob("*.json")) + list(FIXTURES.glob("*.bin")))
    passed = 0
    for path in files:
        expected_accept = path.name.startswith("valid")
        try:
            accepted, checks = check_file(path, validator, limits)
        except Exception as exc:  # noqa: BLE001 - 单个 fixture 异常计为 FAIL
            print("[FAIL] %s (预期 %s) 检查器异常: %s"
                  % (path.name, "accept" if expected_accept else "reject", exc))
            failures += 1
            continue
        ok = accepted == expected_accept
        verdict = "accept" if accepted else "reject"
        # 逐层输出：通过或拒绝原因
        parts = []
        for name, good, why in checks:
            parts.append("%s:ok" % name if good else "%s:REJECT[%s]" % (name, why))
        print("[%s] %s 预期=%s 实际=%s  %s"
              % ("PASS" if ok else "FAIL", path.name,
                 "accept" if expected_accept else "reject", verdict, "  ".join(parts)))
        if ok:
            passed += 1
        else:
            failures += 1

    print()
    print("汇总: %d/%d PASS" % (passed, len(files)))
    if failures:
        print("退出码: 1")
        return 1
    print("退出码: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
