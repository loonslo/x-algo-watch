"""有意只支持已核实的 Rust 子集；遇到新表达式拒绝猜测。"""

import json
import re
from hashlib import sha256
from typing import Any

from .models import Parameter, ParseError

PARAM_PATH = "home-mixer/params/param.rs"
LEVEL_PATH = "visibility-filtering/rules/levels.rs"
FILTER_PATH = "home-mixer/filters/mod.rs"
REQUIRED_LEVELS = {"FILTER_ALL", "TIMELINE_HOME", "TIMELINE_HOME_RECOMMENDATIONS", "TIMELINE_HOME_HYDRATION", "IMMERSIVE_EXPANDED_RECOMMENDATIONS"}


def uncomment(text: str) -> str:
    """保留字符串和行号；支持嵌套块注释。"""
    output: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == '"':
            match = re.match(r'"(?:\\.|[^"\\])*"', text[i:])
            if not match:
                raise ParseError("Rust 字符串未闭合")
            output.append(match[0]); i += len(match[0])
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = len(text) if end == -1 else end
            output.append(" " * (end - i)); i = end
        elif text.startswith("/*", i):
            start = i; depth = 1; i += 2
            while i < len(text) and depth:
                if text.startswith("/*", i):
                    depth += 1; i += 2
                elif text.startswith("*/", i):
                    depth -= 1; i += 2
                else:
                    i += 1
            if depth:
                raise ParseError("Rust 块注释未闭合")
            output.append("".join("\n" if c == "\n" else " " for c in text[start:i]))
        else:
            output.append(text[i]); i += 1
    return "".join(output)


def split_items(text: str) -> list[str]:
    items: list[str] = []; stack: list[str] = []; start = 0; quoted = False; escaped = False
    pairs = {")": "(", "]": "[", "}": "{", ">": "<"}
    for i, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in "([{<":
            stack.append(char)
        elif char in ")]}>":
            if not stack or stack.pop() != pairs[char]:
                raise ParseError("Rust 表达式括号不匹配")
        elif char == "," and not stack:
            items.append(text[start:i].strip()); start = i + 1
    if stack or quoted:
        raise ParseError("Rust 表达式未闭合")
    if text[start:].strip():
        items.append(text[start:].strip())
    return items


def literal(raw: str) -> Any:
    raw = raw.strip()
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"-?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?", raw):
        return json.loads(raw.replace("_", ""))
    string = re.fullmatch(r'("(?:\\.|[^"\\])*")(?:\.to_string\(\)|\.to_owned\(\))?', raw)
    if string:
        try:
            return json.loads(string[1])
        except ValueError as exc:
            raise ParseError("不支持的 Rust 字符串转义") from exc
    vector = re.fullmatch(r"vec!\s*\[(.*)]", raw, re.S)
    if vector:
        return [literal(part) for part in split_items(vector[1])]
    raise ParseError("不支持的默认值表达式")


def parse_params(text: str) -> tuple[dict[str, Parameter], str]:
    clean = uncomment(text)
    params: dict[str, Parameter] = {}
    matches = list(re.finditer(r"\bparam\s*!\s*\(", clean))
    for match in matches:
        i = match.end(); depth = 1; quoted = False; escaped = False
        while i < len(clean) and depth:
            c = clean[i]
            if quoted:
                if escaped: escaped = False
                elif c == "\\": escaped = True
                elif c == '"': quoted = False
            elif c == '"': quoted = True
            elif c == "(": depth += 1
            elif c == ")": depth -= 1
            i += 1
        line = clean.count("\n", 0, match.start()) + 1
        if depth:
            raise ParseError(f"{PARAM_PATH}:{line} param! 未闭合")
        parts = split_items(clean[match.end():i - 1])
        if len(parts) != 4 or not re.fullmatch(r"[A-Za-z_]\w*", parts[0]):
            raise ParseError(f"{PARAM_PATH}:{line} 不认识的 param! 写法")
        name, rust_type, key_raw, raw = parts
        try:
            key = literal(key_raw); value = literal(raw)
        except ParseError as exc:
            raise ParseError(f"{PARAM_PATH}:{line} {name} {exc}") from exc
        if not isinstance(key, str) or name in params or any(p.key == key for p in params.values()):
            raise ParseError(f"{PARAM_PATH}:{line} 参数名或键重复/无效")
        numeric = re.fullmatch(r"[iu](8|16|32|64|128|size)", rust_type)
        valid = ((rust_type == "bool" and type(value) is bool)
                 or (rust_type == "String" and isinstance(value, str))
                 or (numeric and type(value) is int)
                 or (rust_type in ("f32", "f64") and type(value) in (int, float))
                 or (rust_type == "Vec<String>" and isinstance(value, list) and all(isinstance(v, str) for v in value))
                 or (rust_type == "Vec<u64>" and isinstance(value, list) and all(type(v) is int and v >= 0 for v in value)))
        if not valid:
            raise ParseError(f"{PARAM_PATH}:{line} {name} 类型或默认值不支持")
        params[name] = Parameter(name, rust_type, key, value, raw, line)
    if not params or len(matches) != len(re.findall(r"\bparam\s*!", clean)):
        raise ParseError(f"{PARAM_PATH}:1 参数为空或宏写法不支持")
    sync = re.search(r"last sync (\d{4}-\d{2}-\d{2}T[\d:]+Z)", text)
    if not sync:
        raise ParseError(f"{PARAM_PATH}:1 找不到同步时间")
    return params, sync[1]


def rules_list(body: str, refs: bool) -> tuple[str, ...]:
    items = split_items(body)
    pattern = r"&([A-Z][A-Z0-9_]*)" if refs else r"([A-Z][A-Z0-9_]*)"
    result: list[str] = []
    for item in items:
        match = re.fullmatch(pattern, item)
        if not match:
            raise ParseError(f"{LEVEL_PATH} 不认识的规则列表表达式")
        result.append(match[1])
    if len(result) != len(set(result)):
        raise ParseError(f"{LEVEL_PATH} 规则重复")
    return tuple(result)


# 07d159b 的已核实 Expansion::ReactedTo 实现（去注释/空白，不含测试）。
# placed 或 spec 改变时必须重新核实，不能猜测新表达式的名单与顺序。
REACTED_TO_EXPANSION_DIGEST = "6afb1d6d14da0d7bc9d79eb7e4b581ae36e746c3fc2013c9c20f814933eadafc"


def expanded_post_rules(clean: str, name: str, expansion_text: str | None, tweet_rules_text: str | None) -> tuple[str, ...]:
    if expansion_text is None or tweet_rules_text is None:
        raise ParseError(f"{LEVEL_PATH} {name} 缺少展开实现或规则动作来源")
    implementation = uncomment(expansion_text).split("#[cfg(test)]", 1)[0]
    if sha256(re.sub(r"\s+", "", implementation).encode()).hexdigest() != REACTED_TO_EXPANSION_DIGEST:
        raise ParseError(f"{LEVEL_PATH} {name} Expansion 实现尚未核实")
    pattern = (
        r"\bconst\s+" + re.escape(name) + r"\s*:\s*\[\s*&Rule\s*;\s*"
        r"([A-Z][A-Z0-9_]*)\.len\(\)\s*\+\s*Expansion::ReactedTo\.spec\(\)\.rules\.len\(\)\s*\]"
        r"\s*=\s*Expansion::ReactedTo\.placed\(\1\)\s*;"
    )
    matches = re.findall(pattern, clean)
    if len(matches) != 1:
        raise ParseError(f"{LEVEL_PATH} {name} 展开数组写法不支持或重复")
    def array(identifier: str) -> tuple[str, ...]:
        bodies = re.findall(r"\bconst\s+" + re.escape(identifier) + r"\s*:\s*&\s*\[\s*&Rule\s*\]\s*=\s*&\s*\[([^\[\]]*)\]\s*;", clean, re.S)
        if len(bodies) != 1:
            raise ParseError(f"{LEVEL_PATH} {identifier} 引用缺失、重复或写法不支持")
        return rules_list(bodies[0], True)
    parent, expansion = array(matches[0]), array("REACTED_TO_RULES")
    if not parent or set(parent) & set(expansion):
        raise ParseError(f"{LEVEL_PATH} {name} 展开父表为空或规则重复")
    before: list[str] = []
    after: list[str] = []
    rules_source = uncomment(tweet_rules_text)
    for rule in expansion:
        definitions = re.findall(r"pub\(super\)\s+const\s+" + re.escape(rule) + r"\s*:\s*Rule\s*=\s*Rule\s*\{(.*?)\n\};", rules_source, re.S)
        actions = re.findall(r"\baction\s*:\s*(drop_post|blur)\s*\(", definitions[0]) if len(definitions) == 1 else []
        if len(actions) != 1:
            raise ParseError(f"{LEVEL_PATH} {rule} 展开规则动作未知")
        (after if actions[0] == "drop_post" else before).append(rule)
    return (*before, *parent, *after)


def parse_levels(text: str, *, expansion_text: str | None = None, tweet_rules_text: str | None = None) -> tuple[dict[str, tuple[str, ...]], str]:
    clean = uncomment(text)
    levels: dict[str, tuple[str, ...]] = {}
    macro = re.search(r"(?<!\w)levels!\s*\{(.*?)\n\}", clean, re.S)
    if macro:
        pattern = r"([A-Z][A-Z0-9_]*)\s*=\s*\[([^\[\]]*)\]\s*;"
        for match in re.finditer(pattern, macro[1], re.S):
            if match[1] in levels:
                raise ParseError(f"{LEVEL_PATH} 级别重复")
            levels[match[1]] = rules_list(match[2], False)
        if re.sub(pattern, "", macro[1], flags=re.S).strip():
            raise ParseError(f"{LEVEL_PATH} levels! 宏含未知语法")
        style = "levels!"
    else:
        # 只解析顶层常量，严格检查每个声明；不把 struct/enum 的类型字段当列表。
        consts = re.findall(r"pub\(super\)\s+const\s+(\w+)\s*:\s*(.*?)\s*=\s*(.*?);", clean, re.S)
        styles: set[str] = set()
        for name, typ, body in consts:
            if name in levels:
                raise ParseError(f"{LEVEL_PATH} 级别重复")
            if re.fullmatch(r"&\s*\[\s*&\s*(?:Rule|UserRule)\s*\]", typ.strip()):
                match = re.fullmatch(r"&\s*\[([^\[\]]*)\]", body.strip(), re.S)
                if not match:
                    raise ParseError(f"{LEVEL_PATH} {name} 数组写法不支持")
                levels[name] = rules_list(match[1], True)
                styles.add("array")
            elif typ.strip() == "PostRules":
                match = re.fullmatch(r"PostRules\s*\{\s*user:\s*&\s*\[([^\[\]]*)\],\s*post:\s*&\s*(?:\[([^\[\]]*)\]|([A-Z][A-Z0-9_]*)),?\s*\}", body.strip(), re.S)
                if not match:
                    raise ParseError(f"{LEVEL_PATH} {name} PostRules 写法不支持")
                post = rules_list(match[2], True) if match[2] is not None else expanded_post_rules(clean, match[3], expansion_text, tweet_rules_text)
                levels[name] = rules_list(match[1], True) + post
                if match[3]:
                    styles.add("Expansion")
                styles.add("PostRules")
            else:
                raise ParseError(f"{LEVEL_PATH} {name} 未知常量类型")
        style = "PostRules+Expansion" if "Expansion" in styles else ("PostRules" if "PostRules" in styles else "array")
    if not REQUIRED_LEVELS.issubset(levels) or any(not levels[name] for name in REQUIRED_LEVELS):
        raise ParseError(f"{LEVEL_PATH}:1 必需的级别缺失或为空")
    return levels, style


def parse_modules(text: str) -> tuple[str, ...]:
    clean = uncomment(text)
    pattern = r"pub(?:\([^)]*\))?\s+mod\s+([a-z_]\w*)\s*;"
    names = re.findall(pattern, clean)
    if not names or len(names) != len(set(names)) or re.sub(pattern, "", clean).strip():
        raise ParseError(f"{FILTER_PATH}:1 模块列表为空、重复或写法不支持")
    return tuple(names)


def parse_updates(text: str) -> str:
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.M))
    for i, heading in enumerate(headings):
        # 历史已核实的旧标题，避免把早期 README 误报为缺失。
        if heading[1].startswith(("Notable Updates", "Latest update", "Latest Updates")):
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            return text[heading.start():end].strip()
    raise ParseError("README.md:1 找不到 Notable Updates / Latest update 小节")


def guard_count(before: int, after: int, path: str) -> None:
    if after == 0 or after * 2 < before:
        raise ParseError(f"{path}:1 解析数为空或比上一版减少超过一半（{before} → {after}）")
