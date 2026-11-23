"""Versioned Agent Skills. Discovery reads only the SKILL.md front matter."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

_PACKAGED = Path(__file__).with_name("skillpacks")
SKILL_NAMES = {"DS": "data-science", "DE": "domain-expert", "HC": "health-coach"}
_VERSION = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_REFERENCE = re.compile(r"[a-z0-9][a-z0-9.-]*\.md")
DE_SEARCH_ORDERS = ("legacy", "metrics-first", "retry-without-timing")


@dataclass(frozen=True)
class SkillMetadata:
    role: str
    name: str
    description: str
    version: str


@dataclass(frozen=True)
class SkillEvent:
    stage: str
    role: str
    version: str
    resource: str = ""


@dataclass(frozen=True)
class EvidenceSkillPolicy:
    joint_search_order: str = "legacy"

    @classmethod
    def parse(cls, value: object) -> "EvidenceSkillPolicy":
        if (not isinstance(value, dict) or set(value) != {"joint_search_order"}
                or value["joint_search_order"] not in DE_SEARCH_ORDERS):
            raise ValueError("DE 策略只接受受控的 joint_search_order 枚举。")
        return cls(value["joint_search_order"])


class SkillRegistry:
    def __init__(self, root: Path | None = None, versions: dict[str, str] | None = None):
        versions = versions or {}
        if set(versions) - SKILL_NAMES.keys():
            raise ValueError("Skill 角色需要是 DS、DE 或 HC。")
        self._entries = {}
        for role, name in SKILL_NAMES.items():
            version = versions.get(role, "v1")
            if not _VERSION.fullmatch(version):
                raise ValueError(f"无效的 Skill 版本：{version}")
            directory = Path(root) / version / name if root else _PACKAGED / version / name
            if root and version == "v1" and not directory.exists():
                directory = _PACKAGED / version / name
            path = directory / "SKILL.md"
            fields = {}
            with path.open(encoding="utf-8") as file:
                if file.readline().strip() != "---":
                    raise ValueError(f"Skill 缺少元数据：{path}")
                for line in iter(file.readline, ""):
                    if line.strip() == "---":
                        break
                    key, separator, value = line.partition(":")
                    if not separator or key in fields:
                        raise ValueError(f"Skill 元数据格式错误：{path}")
                    fields[key] = value.strip()
                else:
                    raise ValueError(f"Skill 元数据未闭合：{path}")
                body_offset = file.tell()
            if fields.get("name") != name or not fields.get("description"):
                raise ValueError(f"Skill 名称或描述无效：{path}")
            self._entries[role] = (SkillMetadata(role, name, fields["description"], version), path, body_offset)

    @property
    def metadata(self) -> tuple[SkillMetadata, ...]:
        return tuple(entry[0] for entry in self._entries.values())

    def start(self) -> "SkillSession":
        return SkillSession(self)


class SkillSession:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry
        self.events = [SkillEvent("discover", item.role, item.version) for item in registry.metadata]
        self._active: dict[str, ActiveSkill] = {}

    def activate(self, role: str) -> "ActiveSkill":
        if role not in self._active:
            metadata, path, offset = self.registry._entries[role]
            with path.open(encoding="utf-8") as file:
                file.seek(offset)
                body = file.read(12_001).strip()
            if not body or len(body) > 12_000:
                raise ValueError(f"Skill 正文为空或超出长度：{path}")
            self.events.append(SkillEvent("activate", role, metadata.version, "SKILL.md"))
            self._active[role] = ActiveSkill(metadata, body, path.parent, self.events)
        return self._active[role]


class ActiveSkill:
    def __init__(self, metadata: SkillMetadata, body: str, directory: Path, events: list[SkillEvent]):
        self.metadata = metadata
        self.body = body
        self._directory = directory.resolve()
        self._events = events
        self._resources: dict[str, str] = {}

    def _read(self, relative: str) -> str:
        if relative not in self._resources:
            path = (self._directory / relative).resolve()
            if not path.is_relative_to(self._directory):
                raise ValueError("Skill 资源超出版本目录。")
            with path.open(encoding="utf-8") as file:
                content = file.read(8_001)
            if len(content) > 8_000:
                raise ValueError(f"Skill 资源超出长度：{relative}")
            self._resources[relative] = content
            self._events.append(SkillEvent("reference", self.metadata.role, self.metadata.version, relative))
        return self._resources[relative]

    def reference(self, name: str) -> str:
        if not _REFERENCE.fullmatch(name):
            raise ValueError("无效的 Skill 参考资料名。")
        return self._read(f"references/{name}")

    def evidence_policy(self) -> EvidenceSkillPolicy:
        if self.metadata.role != "DE":
            raise ValueError("只有 DE Skill 可以加载证据策略。")
        return EvidenceSkillPolicy.parse(json.loads(self._read("policy.json")))

    def prompt(self, base: str, *, reference: str | None = None) -> str:
        prompt = f"{base}\n\n# {self.metadata.name}\n{self.body}"
        return f"{prompt}\n\n{self.reference(reference)}" if reference else prompt


def skill_prompt(base: str, skill: ActiveSkill | None, *, reference: str | None = None) -> str:
    return skill.prompt(base, reference=reference) if skill else base
