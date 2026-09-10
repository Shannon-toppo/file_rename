# -*- coding: utf-8 -*-
"""検索・置換の文字列マッチ（Qt 非依存の純粋関数。単体テスト対象）。

- 通常は打った文字どおりの部分一致（正規表現の記号もただの文字）
- ワイルドカード指定時は Excel と同じ記法:
  ``*`` = 任意の文字列（0 文字以上）、``?`` = 任意の 1 文字、
  ``~*`` / ``~?`` / ``~~`` = 記号そのもの
- ``*`` は Excel と同じく最長一致。末尾の ``*`` が 0 文字で終わってしまうと
  「feat.*」のような「以降を丸ごと消す」指定が効かないため
- 大文字小文字は既定で区別しない

正規表現はここで組み立てるだけで、利用者に正規表現そのものは書かせない
（記号の打ち間違いで無効なパターンになる、という失敗をさせないため）。
"""
import re
from functools import lru_cache

# ワイルドカードで記号そのものを表すエスケープ文字（Excel と同じ）
WILDCARD_ESCAPE = "~"


def wildcard_to_regex(query: str) -> str:
    """ワイルドカード表記を正規表現の本体へ変換する。"""
    out: list[str] = []
    i = 0
    while i < len(query):
        ch = query[i]
        if ch == WILDCARD_ESCAPE and i + 1 < len(query) and query[i + 1] in "*?~":
            out.append(re.escape(query[i + 1]))
            i += 2
            continue
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


@lru_cache(maxsize=64)
def compile_pattern(
    query: str, wildcard: bool = False, case_sensitive: bool = False
) -> "re.Pattern[str] | None":
    """検索語をパターンへ変換する。空の検索語は None。

    行の絞り込みで行数 × 列数ぶん呼ばれるためキャッシュする。
    """
    if not query:
        return None
    body = wildcard_to_regex(query) if wildcard else re.escape(query)
    flags = re.DOTALL | (0 if case_sensitive else re.IGNORECASE)
    return re.compile(body, flags)


def has_match(text: str, pattern: "re.Pattern[str]") -> bool:
    """text に 1 文字以上の一致があるか（「*」単独などの空一致は数えない）。"""
    return any(m.end() > m.start() for m in pattern.finditer(text))


def replace_in(text: str, pattern: "re.Pattern[str]", replacement: str) -> tuple[str, int]:
    """一致箇所をすべて replacement に置き換え、(置換後, 置換箇所数) を返す。

    replacement はそのままの文字列として扱う（``\\1`` などの後方参照は
    解釈しない）。空一致は置き換えない — re.sub のままだと「*」単独で
    「abc」→「XX」（末尾の空一致にもう 1 個入る）になるため。
    """
    parts: list[str] = []
    last = 0
    count = 0
    for m in pattern.finditer(text):
        if m.end() == m.start():
            continue
        parts.append(text[last : m.start()])
        parts.append(replacement)
        last = m.end()
        count += 1
    if not count:
        return text, 0
    parts.append(text[last:])
    return "".join(parts), count
