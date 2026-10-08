#!/usr/bin/env python3
"""
Business routing policy — where organisation-specific knowledge about *where
documents live* is declared, instead of being hard-coded into `nav/route.py`.

Why this exists
---------------
`nav/route.py` makes four assumptions about how a corpus is arranged:

1. **Periods are years** — `year_hints()` scrapes `(19|20)\\d{2}`.
2. **Folder names are weak evidence** — `_score_candidate()` trusts a directory
   summary more than the folder's own name.
3. **Every directory is equally worth looking in** — no notion of a
   "first place to look".
4. **The question's own words are all we have** — no synonym expansion.

Those are good defaults for an unknown corpus, but inside a company they are
wrong in a specific, knowable way: the finance team *knows* that statutory
annual reports live under `annual/`, that `_drafts/` must never be searched,
that "平安" and "Ping An" are the same company, and that a question about solvency
should start in `regulatory/`.

That knowledge belongs in a config file a business owner can edit, not in a
Python function. This module is the boundary.

Design rules
------------
* **Empty policy is a no-op.** `RoutingPolicy()` changes nothing about routing.
  Every method returns the same answer the old code did. This is the property
  the tests pin down, because it is what makes the feature safe to ship.
* **Config ranks, it does not gate.** Weights and annotations bias the model's
  choice; only `directories.exclude` actually removes candidates, and only
  because a `_drafts/` hit is never right. Nothing here can make an answerable
  question unanswerable.
* **Never fail the query.** A broken policy file raises `PolicyError` at load
  time, which callers catch and degrade to defaults — a typo in YAML must not
  take the server down, but it must be visible in `results/logs/errors.jsonl`.

See `config/routing_policy.yaml` for the file format.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Sequence

from superindex.runtime import app_dir, is_frozen

# Where the policy file is looked up, in order. `SUPERINDEX_ROUTING_POLICY`
# overrides all of them, which is what a deployment uses to keep business
# config out of the checkout.
POLICY_ENV = "SUPERINDEX_ROUTING_POLICY"
POLICY_CANDIDATES = (
    "config/routing_policy.yaml",
    "config/routing_policy.yml",
    "config/routing_policy.json",
)

# The built-in period extractor. Kept here (rather than in route.py) so the
# default and the configured patterns are applied by one code path.
YEAR_RE = re.compile(r"(?:19|20)\d{2}")


class PolicyError(ValueError):
    """The policy file exists but could not be read or made sense of.

    Raised at load time only. Callers are expected to catch it, log it, and
    carry on with an empty policy — see `nav.route._bind_policy`.
    """


# ── value objects ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DirWeight:
    """A directory pattern that should be preferred when scoring candidates.

    `pattern` matches leniently (substring, or fnmatch when it has wildcards)
    against the whole relative path, so `annual` catches `2024/annual` and
    `*/annual/*` works too. `label` is only used to annotate the prompt, so it
    can carry the business meaning the folder name does not.
    """

    pattern: str
    weight: int = 1
    label: str = ""


@dataclass(frozen=True)
class Scope:
    """A business domain and the directories it usually lives in.

    Purely advisory: it is rendered into the routing prompt as a hint. It never
    filters or reorders anything.
    """

    name: str
    dirs: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class ReportType:
    """A kind of report (`report_types:` in the policy file).

    `folders` are keywords of the source folder (the strongest evidence),
    `aliases` the abbreviations / full names that identify it in a file name
    or in a question, `filename` extra file-name-only keywords (too generic
    for questions, e.g. "Monthly"). A type with `split` (folder MBR/QBR) is
    refined by those types' file-name keywords; `use` says what it answers.
    `quarter_as_half`: its Q2 / Q4 mean the half year / full year (1H / FY),
    e.g. a Factbook published half-yearly.
    """

    key: str
    name: str = ""
    folders: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    filename: tuple[str, ...] = ()
    split: tuple[str, ...] = ()
    use: str = ""
    quarter_as_half: bool = False


@dataclass(frozen=True)
class Overlay:
    """Per-corpus additions, merged on top of the global defaults."""

    weights: tuple[DirWeight, ...] = ()
    exclude: tuple[str, ...] = ()
    scopes: tuple[Scope, ...] = ()
    instructions: str = ""


# ── parsing helpers ──────────────────────────────────────────────────────
def _norm(value: str) -> str:
    """Path/pattern normal form: no surrounding slashes, case-insensitive."""
    return (value or "").strip().strip("/").lower()


def _as_text(value: Any, key: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    raise PolicyError(f"{key}: 期望字符串，得到 {type(value).__name__} ({value!r})")


def _as_str_tuple(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for i, v in enumerate(value):
            if not isinstance(v, str) or not v.strip():
                raise PolicyError(f"{key}[{i}]: 期望非空字符串，得到 {v!r}")
            out.append(v.strip())
        return tuple(out)
    raise PolicyError(f"{key}: 期望字符串或字符串列表，得到 {type(value).__name__}")


def _as_bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise PolicyError(f"{key}: 期望 true / false，得到 {type(value).__name__}")
    return value


def _as_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(f"{key}: 期望数字，得到 {value!r}")
    return int(value)


def _parse_weights(raw: Any, key: str) -> tuple[DirWeight, ...]:
    """Accept both the short form (`- annual`) and the long form.

    The short form matters: most of the time the pattern *is* the whole story,
    and forcing `{pattern: annual, weight: 1}` for that would make the common
    case the ugly one.
    """
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise PolicyError(f"{key}: 期望列表，得到 {type(raw).__name__}")
    out: list[DirWeight] = []
    for i, item in enumerate(raw):
        where = f"{key}[{i}]"
        if isinstance(item, str):
            if not item.strip():
                raise PolicyError(f"{where}: 目录模式不能为空")
            out.append(DirWeight(pattern=item.strip()))
            continue
        if not isinstance(item, Mapping):
            raise PolicyError(f"{where}: 期望字符串或映射，得到 {type(item).__name__}")
        pattern = item.get("pattern", item.get("path", item.get("dir")))
        if not isinstance(pattern, str) or not pattern.strip():
            raise PolicyError(f"{where}: 缺少 pattern（或 path / dir）")
        out.append(DirWeight(
            pattern=pattern.strip(),
            weight=_as_int(item.get("weight", 1), f"{where}.weight"),
            label=_as_text(item.get("label"), f"{where}.label"),
        ))
    return tuple(out)


def _parse_scopes(raw: Any, key: str) -> tuple[Scope, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise PolicyError(f"{key}: 期望列表，得到 {type(raw).__name__}")
    out: list[Scope] = []
    for i, item in enumerate(raw):
        where = f"{key}[{i}]"
        if not isinstance(item, Mapping):
            raise PolicyError(f"{where}: 期望映射（name / dirs / note）")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise PolicyError(f"{where}: 缺少 name")
        out.append(Scope(
            name=name.strip(),
            dirs=_as_str_tuple(item.get("dirs"), f"{where}.dirs"),
            note=_as_text(item.get("note"), f"{where}.note"),
        ))
    return tuple(out)


def _parse_aliases(raw: Any, key: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """`{平安: [Ping An, 中国平安]}` — or a list of `{terms: [...]}` groups.

    Deliberately symmetric: if *any* member of a group appears in the question,
    every other member becomes a search term. Direction would be arbitrary —
    which spelling is "canonical" is not something the user should have to
    decide in order to get synonym matching.
    """
    if raw is None:
        return ()
    items: list[tuple[str, tuple[str, ...]]] = []
    if isinstance(raw, Mapping):
        for k, v in raw.items():
            if not isinstance(k, str) or not k.strip():
                raise PolicyError(f"{key}: 别名组的键必须是非空字符串，得到 {k!r}")
            items.append((k.strip(), _as_str_tuple(v, f"{key}.{k}")))
    elif isinstance(raw, (list, tuple)):
        for i, group in enumerate(raw):
            terms = _as_str_tuple(group, f"{key}[{i}]")
            if not terms:
                raise PolicyError(f"{key}[{i}]: 别名组至少要有 1 个词")
            items.append((terms[0], terms[1:]))
    else:
        raise PolicyError(f"{key}: 期望映射或列表，得到 {type(raw).__name__}")
    return tuple(items)


def _parse_report_types(raw: Any, key: str) -> tuple[ReportType, ...]:
    """`{qmr: {name, folders, aliases, filename, split, use, quarter_as_half}}`, in file order
    (the first type whose folder / file-name keyword matches wins)."""
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise PolicyError(f"{key}: 期望映射（类型键 → name / folders / aliases / use）")
    out: list[ReportType] = []
    for k, body in raw.items():
        if not isinstance(k, str) or not k.strip():
            raise PolicyError(f"{key}: 类型键必须是非空字符串，得到 {k!r}")
        body = body or {}
        if not isinstance(body, Mapping):
            raise PolicyError(f"{key}.{k}: 期望映射")
        where = f"{key}.{k}"
        out.append(ReportType(
            key=k.strip().lower(),
            name=_as_text(body.get("name"), f"{where}.name"),
            folders=_as_str_tuple(body.get("folders"), f"{where}.folders"),
            aliases=_as_str_tuple(body.get("aliases"), f"{where}.aliases"),
            filename=_as_str_tuple(body.get("filename"), f"{where}.filename"),
            split=tuple(s.lower() for s in _as_str_tuple(body.get("split"), f"{where}.split")),
            use=_as_text(body.get("use"), f"{where}.use"),
            quarter_as_half=_as_bool(body.get("quarter_as_half", False),
                                     f"{where}.quarter_as_half"),
        ))
    keys = {t.key for t in out}
    for t in out:
        if unknown := [s for s in t.split if s not in keys]:
            raise PolicyError(f"{key}.{t.key}.split: 未定义的类型 {unknown}")
    return tuple(out)


def _words(text: str) -> str:
    """Lower case, `_` / `-` / runs of spaces as one space (file and folder
    names write "MBR QBR_Finance  Part" as freely as "mbr-qbr finance part")."""
    return " ".join(re.sub(r"[_\-]+", " ", text or "").lower().split())


def _term_in(term: str, text: str, whole: bool = True) -> bool:
    """`term` in `text` (both `_words`): ASCII terms on letter boundaries —
    at the end too when `whole` (a folder keyword may be a word's start:
    "country trend" in "country trends") — others as a substring."""
    term = _words(term)
    if not term:
        return False
    if not term.isascii():
        return term in text
    tail = r"(?![a-z])" if whole else ""
    return re.search(rf"(?<![a-z]){re.escape(term)}{tail}", text) is not None


@lru_cache(maxsize=64)
def _compile_periods(patterns: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p))
        except re.error as exc:
            raise PolicyError(f"periods.patterns: 正则 {p!r} 无效 — {exc}") from exc
    return tuple(out)


def _parse_periods(raw: Any, key: str) -> tuple[str, ...]:
    """Validate the patterns at load time, not at query time.

    A bad regex must surface while the user is looking at the file they just
    edited, not as a traceback during someone else's question.
    """
    patterns = _as_str_tuple(raw, key)
    _compile_periods(patterns)
    return patterns


# ── matching ─────────────────────────────────────────────────────────────
def _pattern_hits_path(pattern: str, rel_path: str) -> bool:
    """Lenient match, used for *weights*.

    Weights only ever add a bonus, so a loose match is cheap and a miss is
    expensive (a directory the business cares about silently not getting its
    bonus). Substring is therefore the default, with wildcards available.
    """
    pat, path = _norm(pattern), _norm(rel_path)
    if not pat or not path:
        return False
    if any(ch in pat for ch in "*?["):
        return (fnmatch.fnmatchcase(path, pat)
                or any(fnmatch.fnmatchcase(seg, pat) for seg in path.split("/")))
    return pat in path


def _pattern_hits_segment(pattern: str, rel_path: str) -> bool:
    """Strict match, used for *exclusions*.

    An exclusion is the one thing here that can hide data, so it must not fire
    on a substring: `exclude: [draft]` should not delete a real
    `drafting-guidelines/` directory from the candidate list. A pattern with a
    `/` is treated as a path pattern instead, which is how you write a
    deliberately deep rule.
    """
    pat, path = _norm(pattern), _norm(rel_path)
    if not pat or not path:
        return False
    if "/" in pat:
        return (fnmatch.fnmatchcase(path, pat)
                or path.startswith(pat.rstrip("/") + "/"))
    return any(fnmatch.fnmatchcase(seg, pat) for seg in path.split("/"))


def _shipped_root() -> Path:
    """Where `config/routing_policy.yaml` ships: the PyInstaller bundle
    (packaging/superindex.spec copies it there) or the source checkout."""
    if is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


def _resolve_path(path: Optional[str | Path]) -> Optional[Path]:
    """Find the policy file, or None if there is none (which is normal)."""
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise PolicyError(f"策略文件不存在: {p}")
        return p
    env = os.getenv(POLICY_ENV, "").strip()
    if env:
        p = Path(env)
        if not p.is_file():
            raise PolicyError(f"{POLICY_ENV}={env} 指向的文件不存在")
        return p
    # Relative candidates are resolved against the CWD, then the app folder
    # (the executable's folder when frozen, like `.env`), then the copy that
    # ships with the code: the repo root from source, the bundle when frozen.
    # So an edited policy next to the app wins over the shipped default.
    bases = [Path.cwd(), app_dir(), _shipped_root()]
    for base in bases:
        for cand in POLICY_CANDIDATES:
            p = base / cand
            if p.is_file():
                return p
    return None


def _read(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"{path} JSON 解析失败: {exc}") from exc
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is a pinned dep
        raise PolicyError(
            f"{path} 是 YAML 配置，但当前环境没有 PyYAML。"
            f"请 `pip install pyyaml`，或改用同名的 .json 文件。"
        ) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path} YAML 解析失败: {exc}") from exc


# ── the policy itself ────────────────────────────────────────────────────
@dataclass(frozen=True)
class RoutingPolicy:
    """Resolved routing policy. Immutable; `flatten_for` returns new instances."""

    weights: tuple[DirWeight, ...] = ()
    exclude: tuple[str, ...] = ()
    scopes: tuple[Scope, ...] = ()
    periods: tuple[str, ...] = ()
    aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()
    instructions: str = ""
    corpora: tuple[tuple[str, Overlay], ...] = ()
    report_types: tuple[ReportType, ...] = ()
    source: str = ""

    # ---- construction --------------------------------------------------
    @staticmethod
    def load(path: Optional[str | Path] = None) -> "RoutingPolicy":
        """Read the policy from disk.

        No file present is the normal case and yields an empty policy. A file
        that is present but broken raises `PolicyError` — a silently ignored
        typo is worse than a loud failure, because the user would edit the file
        and never understand why nothing changed.
        """
        found = _resolve_path(path)
        if found is None:
            return RoutingPolicy()
        return RoutingPolicy.from_dict(_read(found), source=str(found))

    @staticmethod
    def from_dict(raw: Any, source: str = "") -> "RoutingPolicy":
        if raw is None:
            return RoutingPolicy(source=source)
        if not isinstance(raw, Mapping):
            raise PolicyError(
                f"策略根节点必须是映射（key: value），得到 {type(raw).__name__}")

        dirs = raw.get("directories") or {}
        if not isinstance(dirs, Mapping):
            raise PolicyError("directories: 期望映射（weights / exclude / scopes）")
        periods = raw.get("periods") or {}
        if isinstance(periods, (list, tuple, str)):
            periods = {"patterns": periods}
        if not isinstance(periods, Mapping):
            raise PolicyError("periods: 期望映射（patterns: [...]）")

        corpora_raw = raw.get("corpora") or {}
        if not isinstance(corpora_raw, Mapping):
            raise PolicyError("corpora: 期望映射（语料名/ID → 覆盖项）")
        corpora: list[tuple[str, Overlay]] = []
        for key, body in corpora_raw.items():
            if not isinstance(key, str) or not key.strip():
                raise PolicyError(f"corpora: 语料键必须是非空字符串，得到 {key!r}")
            if body is None:
                body = {}
            if not isinstance(body, Mapping):
                raise PolicyError(f"corpora.{key}: 期望映射")
            cdirs = body.get("directories") or {}
            if not isinstance(cdirs, Mapping):
                raise PolicyError(f"corpora.{key}.directories: 期望映射")
            corpora.append((key.strip(), Overlay(
                weights=_parse_weights(
                    cdirs.get("weights"), f"corpora.{key}.directories.weights"),
                exclude=_as_str_tuple(
                    cdirs.get("exclude"), f"corpora.{key}.directories.exclude"),
                scopes=_parse_scopes(
                    cdirs.get("scopes"), f"corpora.{key}.directories.scopes"),
                instructions=_as_text(body.get("instructions"),
                                      f"corpora.{key}.instructions"),
            )))

        return RoutingPolicy(
            weights=_parse_weights(dirs.get("weights"), "directories.weights"),
            exclude=_as_str_tuple(dirs.get("exclude"), "directories.exclude"),
            scopes=_parse_scopes(dirs.get("scopes"), "directories.scopes"),
            periods=_parse_periods(periods.get("patterns"), "periods.patterns"),
            aliases=_parse_aliases(raw.get("aliases"), "aliases"),
            instructions=_as_text(raw.get("instructions"), "instructions"),
            corpora=tuple(corpora),
            report_types=_parse_report_types(raw.get("report_types"), "report_types"),
            source=source,
        )

    # ---- introspection -------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return not (self.weights or self.exclude or self.scopes or self.periods
                    or self.aliases or self.instructions.strip() or self.corpora
                    or self.report_types)

    @property
    def corpus_keys(self) -> tuple[str, ...]:
        return tuple(k for k, _ in self.corpora)

    def describe(self) -> str:
        if self.is_empty:
            return "空策略（行为与内置默认一致）"
        bits = []
        if self.weights:
            bits.append(f"{len(self.weights)} 条目录权重")
        if self.exclude:
            bits.append(f"{len(self.exclude)} 条排除")
        if self.scopes:
            bits.append(f"{len(self.scopes)} 个业务域")
        if self.periods:
            bits.append(f"{len(self.periods)} 条期间模式")
        if self.aliases:
            bits.append(f"{len(self.aliases)} 组别名")
        if self.instructions.strip():
            bits.append("提示词补充")
        if self.corpora:
            bits.append(f"{len(self.corpora)} 个语料覆盖")
        if self.report_types:
            bits.append(f"{len(self.report_types)} 种报告类型")
        text = "、".join(bits)
        return f"{text}（来自 {self.source}）" if self.source else text

    # ---- per-corpus resolution -----------------------------------------
    def _overlay(self, key: str) -> Optional[Overlay]:
        want = (key or "").strip().lower()
        if not want:
            return None
        for k, ov in self.corpora:
            if k.lower() == want:
                return ov
        return None

    def flatten_for(self, key: str) -> "RoutingPolicy":
        """Global defaults with one corpus's overlay applied.

        Overlays are merged by *concatenation*, not replacement: a corpus that
        adds one exclusion keeps every global one. Replacing would mean every
        corpus had to restate the whole policy, which is exactly the
        copy-paste drift the global layer exists to prevent.
        """
        ov = self._overlay(key)
        if ov is None:
            return replace(self, corpora=()) if self.corpora else self
        return replace(
            self,
            weights=self.weights + ov.weights,
            exclude=self.exclude + ov.exclude,
            scopes=self.scopes + ov.scopes,
            instructions="\n".join(
                x for x in (self.instructions, ov.instructions) if x.strip()),
            corpora=(),
        )

    def bind_corpora(self, id_to_name: Mapping[str, str]) -> "RoutingPolicy":
        """Rewrite overlay keys from display names to internal corpus ids.

        The config is written by humans, so `corpora:` is keyed by the corpus
        name shown in the UI ("友邦保险"). Merged routing paths are keyed by the
        internal corpus id (`a9e87d72`). This binds the two once, at navigator
        construction, so every later path lookup is a plain string compare.

        An overlay naming a corpus that is not registered is left alone rather
        than dropped — it should start working the moment that corpus is added,
        without the user having to remember to re-add it.
        """
        if not self.corpora or not id_to_name:
            return self
        name_to_id = {name: cid for cid, name in id_to_name.items()}
        out: list[tuple[str, Overlay]] = []
        for key, ov in self.corpora:
            out.append((name_to_id.get(key, key), ov))
        return replace(self, corpora=tuple(out))

    def for_path(self, rel_path: str) -> "RoutingPolicy":
        """The flattened policy that governs one path.

        In a merged multi-corpus tree every path is `<corpus_id>/…`, so the
        first segment identifies which overlay applies. This is why overlays
        can be keyed by a human-readable corpus name in the config file and
        still work: `MultiNavigator` binds names to ids once at construction.
        """
        if not self.corpora:
            return self
        head = (rel_path or "").split("/", 1)[0]
        if not head:
            return self
        return self.flatten_for(head)

    # ---- injection point 1: period detection ---------------------------
    def periods_in(self, question: str) -> list[str]:
        """Reporting periods named in the question, most-trusted first.

        Built-in years come first so the common case is unchanged, then any
        configured pattern. Each match contributes at most two terms:

        * a **normalised 4-digit year**, from a 2- or 4-digit capture group —
          `FY24` and `2024` must both match a path holding `2024`, because the
          path was written by a human too;
        * the **literal text**, but only when it carries a label (`FY24`, `Q3`,
          `2024H1`). A bare `2024年` is dropped: no path contains it, so adding
          it only pads the term list and slows matching down.

        Groups of other lengths (a lone `3` out of `Q3`) are ignored for the
        same reason — they match far too much to be useful.
        """
        text = question or ""
        out: list[str] = []

        def add(value: str) -> None:
            value = (value or "").strip()
            if value and value not in out:
                out.append(value)

        for year in YEAR_RE.findall(text):
            add(year)
        for pat in _compile_periods(self.periods):
            for m in pat.finditer(text):
                group = next((g for g in m.groups() if g), None)
                if group and group.isdigit() and len(group) in (2, 4):
                    add(group if len(group) == 4 else f"20{group}")
                raw = re.sub(r"\s+", "", m.group(0))
                if raw and re.search(r"[A-Za-z]", raw):
                    add(raw)
        return out

    # ---- injection point 2: ranking ------------------------------------
    def weight_for(self, rel_path: str) -> int:
        """Total priority bonus for a directory or file path.

        Summed rather than maxed so two signals stack (`annual` *and*
        `group`), which is how a business actually thinks about priority.
        """
        pol = self.for_path(rel_path)
        return sum(w.weight for w in pol.weights
                   if _pattern_hits_path(w.pattern, rel_path))

    def label_for(self, rel_path: str) -> str:
        """Label of the strongest matching weight, for prompt annotation."""
        pol = self.for_path(rel_path)
        best: Optional[DirWeight] = None
        for w in pol.weights:
            if not _pattern_hits_path(w.pattern, rel_path):
                continue
            if best is None or w.weight > best.weight:
                best = w
        if best is None:
            return ""
        return best.label or best.pattern

    def annotate(self, rel_path: str) -> str:
        """Prompt suffix marking a candidate as business-preferred.

        Appended to the candidate's own line, so the hint sits next to the
        choice it is meant to influence instead of in a block the model may
        skim past.
        """
        weight = self.weight_for(rel_path)
        if weight <= 0:
            return ""
        label = self.label_for(rel_path)
        return f"  [优先+{weight}{' ' + label if label else ''}]"

    # ---- injection point 3: prompt -------------------------------------
    def prompt_block(self, rel_paths: Optional[Sequence[str]] = None) -> str:
        """Business guidance to append to a routing prompt.

        Returns "" for an empty policy, so the prompt is byte-identical to the
        pre-policy version unless someone configured something.
        """
        lines: list[str] = []
        if self.instructions.strip():
            lines.append(self.instructions.strip())

        scopes = list(self.scopes)
        if rel_paths is not None and self.corpora:
            heads = {(p or "").split("/", 1)[0].lower() for p in rel_paths if p}
            for key, ov in self.corpora:
                if key.lower() not in heads:
                    continue
                scopes += list(ov.scopes)
                if ov.instructions.strip():
                    lines.append(ov.instructions.strip())
        for s in scopes:
            if s.dirs:
                lines.append(f"- 业务域「{s.name}」优先目录: {'、'.join(s.dirs)}"
                             + (f" —— {s.note}" if s.note else ""))
            else:
                lines.append(f"- 业务域「{s.name}」" + (f"—— {s.note}" if s.note else ""))

        if not lines:
            return ""
        # Name the file that is actually in effect: a deployment can point
        # SUPERINDEX_ROUTING_POLICY somewhere else, and a log that names the
        # wrong file is worse than naming none.
        where = Path(self.source).name if self.source else POLICY_CANDIDATES[0]
        return (f"\n业务检索偏好（来自 {where}，仅作参考，不改变编号规则）:\n"
                + "\n".join(lines) + "\n")

    # ---- injection point 4: fallback -----------------------------------
    def is_excluded(self, rel_path: str) -> bool:
        """True if this candidate must never be routed to.

        The only hard filter in the policy. It exists for directories that are
        structurally wrong to search (`_drafts`, `templates`, `.trash`) rather
        than merely low-priority.
        """
        pol = self.for_path(rel_path)
        return any(_pattern_hits_segment(p, rel_path) for p in pol.exclude)

    def alias_terms(self, question: str) -> list[tuple[str, int]]:
        """Extra search terms from alias groups the question touched.

        Only the members *absent* from the question are returned — the ones
        already present are matched by the normal term extraction, and adding
        them again would just double-count a hit. Weight 2 matches a whole
        phrase, below the year signal's 3.
        """
        low = (question or "").lower()
        out: list[tuple[str, int]] = []
        seen: set[str] = set()
        for key, variants in self.aliases:
            group = (key,) + tuple(variants)
            if not any(g.lower() in low for g in group):
                continue
            for g in group:
                gl = g.lower()
                if gl in low or gl in seen:
                    continue
                seen.add(gl)
                out.append((g, 2))
        return out

    # ---- injection point 5: report types -------------------------------
    def report_type(self, key: str | None) -> ReportType | None:
        want = (key or "").strip().lower()
        return next((t for t in self.report_types if t.key == want), None)

    def quarter_as_half(self, key: str | None) -> bool:
        """Whether type `key` reads Q2 / Q4 as 1H / FY (`ReportType.quarter_as_half`)."""
        found = self.report_type(key)
        return bool(found and found.quarter_as_half)

    def report_type_from_name(self, name: str,
                              among: Sequence[str] | None = None) -> str | None:
        """The first type (of `among`, default all) whose alias or file-name
        keyword is in the file name `name` (or any short text, e.g. an LLM's
        "QMR"), or whose key is the whole text."""
        text = _words(Path(name).stem if Path(name).suffix.lower() in (
            ".pdf", ".md", ".markdown") else name)
        for t in self.report_types:
            if among is not None and t.key not in among:
                continue
            if text == _words(t.key) or any(_term_in(a, text) for a in (*t.aliases, *t.filename)):
                return t.key
        return None

    def report_type_for(self, source_path: str | None,
                        name: str) -> tuple[str | None, str | None]:
        """(type, "folder" | "filename") of a document: a folder of its
        `source_path` (relative to the PDF root) holding a type's folder
        keyword, refined by the file name for a type with `split`; else the
        file name's aliases; (None, None) when neither tells."""
        folders = [_words(seg) for seg in str(source_path or "").replace("\\", "/").split("/")[:-1]]
        for seg in folders:
            for t in self.report_types:
                if any(_term_in(k, seg, whole=False) for k in t.folders):
                    subs = {s for s in t.split if self.report_type_from_name(name, (s,))}
                    return (subs.pop() if len(subs) == 1 else t.key), "folder"
        found = self.report_type_from_name(name)
        return (found, "filename") if found else (None, None)

    def report_types_in(self, question: str) -> list[str]:
        """Types the question names by an alias (abbreviation or full name)."""
        text = _words(question)
        return [t.key for t in self.report_types if any(_term_in(a, text) for a in t.aliases)]

    def covering_types(self, named: Sequence[str]) -> set[str]:
        """`named` plus the types that split into one of them (MBR → MBR/QBR)."""
        return set(named) | {t.key for t in self.report_types if set(t.split) & set(named)}

    def report_types_block(self) -> str:
        """Agent guidance on the configured report types; "" without any."""
        if not self.report_types:
            return ""
        lines = [f"- {t.key}: {t.name or t.key}" + (f" — {t.use}" if t.use else "")
                 for t in self.report_types]
        return ("REPORT TYPES:\n"
                "browse_documents() shows each document's [report_type · period]; pass "
                "`report_type` to list one type. Questions rarely name the type — choose "
                "it from what is asked:\n" + "\n".join(lines)
                + "\n- A document may hold earlier periods (trends, comparatives): the period "
                "is its own reporting period, not the only one it covers.")


__all__ = ["DirWeight", "Overlay", "PolicyError", "ReportType", "RoutingPolicy", "Scope",
           "POLICY_ENV", "YEAR_RE"]
