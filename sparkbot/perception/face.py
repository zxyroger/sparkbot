"""人脸库：把设备端推理出的 512 维人脸特征与「名字」绑定起来。

分工
----
    设备（ESP32-S3）：JPEG → 人脸检测 → 512 维特征向量（本地推理，esp-dl）
    PC（Agent）    ：特征向量 × 已登记的人 → 余弦相似度 → 姓名

**「谁是谁」这件事完全留在 PC 侧。** 设备只回特征和框，不存任何隐私数据；
人脸库就是一个可以随时删掉的 JSON 文件，删了等于所有人脸都没登记过。

为什么不让设备自己认（esp-who 的 HumanFaceRecognizer 也是这么做的）：
那样名字要写进设备 flash，增删改都要走串口/网络下发，而且设备没有屏幕
可以确认"这是谁"。放到 PC 侧以后，绑定姓名只是写一个字典，还能直接接着
长期记忆、对话上下文一起用。

相似度与阈值
------------
    特征在设备端已经做过 L2 归一化（esp-dl 的 ``FeatPostprocessor``），
    所以**点积就是余弦相似度**，越大越像。
    阈值默认 ``0.5`` —— 直接沿用设备端 ``HumanFaceRecognizer`` 的默认阈值：
    同一个模型、同一套特征，判据没有理由不一致。
    （注意：不同人脸模型之间的相似度尺度不可比，换模型必须重新标定。）

存储格式
--------
    JSON。特征按 **float32 小端 base64** 存，与设备上报的格式一致：
    512 维如果写成 JSON 数组要 ~9KB 文本，base64 只要 2.7KB。
    文件很小，所以整体重写 + 原子替换，不做增量写入。
"""

from __future__ import annotations

import base64
import json
import logging
import math
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: 人脸特征维度。与固件的 ``BOT_FACE_FEAT_LEN`` 必须一致，
#: 不一致说明固件和 PC 侧版本对不上 —— 这里挡一道，避免"静默认错人"。
FEAT_LEN = 512

#: 名字长度上限（防止模型/用户塞进来一整句话当名字）。
MAX_NAME_LEN = 24


# --------------------------------------------------------------------------- #
# 向量工具（纯 Python，不引入 numpy）
# --------------------------------------------------------------------------- #
def decode_feat_b64(text: str) -> list[float]:
    """把设备上报的 base64 float32 特征解码成浮点列表。

    坏数据一律返回空列表而不是抛异常：一次解码失败不该让整轮对话失败。
    """
    if not text:
        return []
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return []
    if len(raw) % 4 != 0:
        return []
    count = len(raw) // 4
    if count == 0:
        return []
    return list(struct.unpack(f"<{count}f", raw))


def encode_feat_b64(feat: Iterable[float]) -> str:
    """把浮点特征编码成 base64 float32（与设备上报格式一致）。"""
    values = list(feat)
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")


def l2_normalize(feat: list[float]) -> list[float]:
    """把向量归一化到单位长度。

    设备侧已经归一化过，这里只是**防守**：万一特征全零（模型异常）或
    来自别的来源，不做归一化的话点积就不是余弦相似度了。
    """
    norm = math.sqrt(sum(v * v for v in feat))
    if norm <= 1e-12:
        return []
    return [v / norm for v in feat]


def cosine(a: list[float], b: list[float]) -> float:
    """两个等长向量的点积（= 已归一化向量的余弦相似度）。"""
    if not a or len(a) != len(b):
        return -1.0
    return sum(x * y for x, y in zip(a, b))


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class FaceObservation:
    """设备上报的一张脸（识别结果 + 特征）。"""

    box: tuple[int, int, int, int]
    score: float
    feat: list[float]

    def to_dict(self, *, include_feat: bool = False) -> dict[str, Any]:
        """转成可序列化字典；默认不带特征（特征很大且没有展示价值）。"""
        payload: dict[str, Any] = {
            "box": list(self.box),
            "score": round(float(self.score), 4),
            "feat_len": len(self.feat),
        }
        if include_feat:
            payload["feat"] = self.feat
        return payload


@dataclass(slots=True)
class FaceScan:
    """一次人脸扫描的结果（一帧里可能有多张脸）。"""

    faces: list[FaceObservation] = field(default_factory=list)
    width: int = 0
    height: int = 0

    def __len__(self) -> int:
        return len(self.faces)

    def to_dict(self) -> dict[str, Any]:
        """转成控制台/工具可用的字典。"""
        return {
            "count": len(self.faces),
            "width": self.width,
            "height": self.height,
            "faces": [f.to_dict() for f in self.faces],
        }


@dataclass(slots=True)
class FaceMatch:
    """一张脸与某个人名的匹配结果。"""

    name: str | None
    similarity: float
    box: tuple[int, int, int, int] | None = None

    @property
    def known(self) -> bool:
        """是否匹配到了已登记的人。"""
        return self.name is not None

    def to_dict(self) -> dict[str, Any]:
        """转成可序列化字典。"""
        payload: dict[str, Any] = {
            "name": self.name,
            "similarity": round(float(self.similarity), 4),
            "known": self.known,
        }
        if self.box is not None:
            payload["box"] = list(self.box)
        return payload


# --------------------------------------------------------------------------- #
# 人脸库
# --------------------------------------------------------------------------- #
class FaceDB:
    """姓名 ↔ 人脸特征 的持久化库，进程内线程安全。

    一个人可以有多条特征（不同角度、不同光线各存一条），匹配时取最高分。
    为什么不等价于"存平均向量"：人脸特征是超球面上的点，平均之后模长会
    缩水、方向被拉向"所有人脸的中心"，反而更容易认错。多存几条更稳。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        threshold: float = 0.5,
        max_samples: int = 8,
        enabled: bool = True,
    ) -> None:
        self.path = Path(path)
        self.threshold = float(threshold)
        self.max_samples = max(1, int(max_samples))
        self.enabled = enabled
        self._lock = threading.RLock()
        #: name -> {"samples": [feat, ...], "created_at":..., "updated_at":..., "hits":int}
        self._people: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self.write_errors = 0
        if enabled:
            self.load()

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    def load(self) -> int:
        """从磁盘加载，返回人数。文件不存在很正常（第一次运行）。"""
        with self._lock:
            self._people.clear()
            if not self.path.is_file():
                return 0
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("人脸库读取失败 %s: %s", self.path, exc)
                return 0

            people = raw.get("people") if isinstance(raw, dict) else None
            if not isinstance(people, dict):
                return 0

            for name, entry in people.items():
                if not isinstance(entry, dict):
                    continue
                samples: list[list[float]] = []
                for encoded in entry.get("samples") or []:
                    feat = decode_feat_b64(encoded) if isinstance(encoded, str) else []
                    if len(feat) == FEAT_LEN:
                        samples.append(feat)
                if not samples:
                    continue
                self._people[str(name)] = {
                    "samples": samples,
                    "created_at": float(entry.get("created_at") or time.time()),
                    "updated_at": float(entry.get("updated_at") or time.time()),
                    "hits": int(entry.get("hits") or 0),
                }
            logger.info("人脸库已加载: %d 人 / %d 条特征（%s）",
                        len(self._people), self.sample_count(), self.path)
            return len(self._people)

    def persist(self) -> bool:
        """原子写盘（先写临时文件再替换），失败只记日志。"""
        if not self.enabled:
            return False
        with self._lock:
            if not self._dirty:
                return True
            payload = {
                "version": 1,
                "feat_format": "float32",
                "feat_len": FEAT_LEN,
                "threshold": self.threshold,
                "people": {
                    name: {
                        "samples": [encode_feat_b64(f) for f in entry["samples"]],
                        "created_at": entry["created_at"],
                        "updated_at": entry["updated_at"],
                        "hits": entry["hits"],
                    }
                    for name, entry in self._people.items()
                },
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(self.path)
                self._dirty = False
                return True
            except OSError as exc:
                self.write_errors += 1
                logger.warning("人脸库写盘失败: %s", exc)
                return False

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def enroll(self, name: str, feat: list[float], *, source: str = "agent") -> dict[str, Any] | None:
        """登记一个人（或给已登记的人补一条特征）。

        Args:
            name: 姓名/称呼，会做长度与空白归一化。
            feat: 512 维特征（未归一化也可以，这里会补归一化）。
            source: 来源标记，便于排查是谁写进来的。

        Returns:
            写入后的条目摘要；名字为空或特征长度不对时返回 ``None``。
        """
        cleaned = " ".join((name or "").split())[:MAX_NAME_LEN]
        if not cleaned or not self.enabled:
            return None
        vector = l2_normalize(feat) if len(feat) == FEAT_LEN else []
        if not vector:
            return None

        now = time.time()
        with self._lock:
            entry = self._people.get(cleaned)
            if entry is None:
                entry = {"samples": [], "created_at": now, "updated_at": now, "hits": 0}
                self._people[cleaned] = entry
            # 重复特征不再存（同一张脸连拍两次的情况很常见）
            if not any(cosine(vector, old) > 0.98 for old in entry["samples"]):
                entry["samples"].append(vector)
            # 超出上限时丢最旧的（列表头部是旧的）
            while len(entry["samples"]) > self.max_samples:
                entry["samples"].pop(0)
            entry["updated_at"] = now
            entry["source"] = source
            self._dirty = True
            summary = self._summary(cleaned, entry)
        self.persist()
        return summary

    def remove(self, name: str) -> bool:
        """删掉一个人。"""
        with self._lock:
            if name not in self._people:
                return False
            del self._people[name]
            self._dirty = True
        self.persist()
        return True

    def clear(self) -> int:
        """清空整个人脸库，返回删掉的人数。"""
        with self._lock:
            n = len(self._people)
            self._people.clear()
            self._dirty = True
        self.persist()
        return n

    # ------------------------------------------------------------------ #
    # 匹配
    # ------------------------------------------------------------------ #
    def match(self, feat: list[float]) -> FaceMatch:
        """给一张脸找名字；认不出时 ``name`` 为 ``None``。"""
        if not self.enabled or len(feat) != FEAT_LEN:
            return FaceMatch(name=None, similarity=-1.0)
        vector = l2_normalize(feat)
        if not vector:
            return FaceMatch(name=None, similarity=-1.0)

        best_name: str | None = None
        best_sim = -1.0
        with self._lock:
            for name, entry in self._people.items():
                for old in entry["samples"]:
                    sim = cosine(vector, old)
                    if sim > best_sim:
                        best_sim = sim
                        best_name = name
            if best_name is not None and best_sim >= self.threshold:
                self._people[best_name]["hits"] += 1
            else:
                best_name = None

        if best_name is not None:
            self.persist()
        return FaceMatch(name=best_name, similarity=best_sim)

    def match_scan(self, faces: Iterable[FaceObservation]) -> list[FaceMatch]:
        """给一次扫描里的每张脸找名字。

        多张脸时按相似度从高到低返回 —— 对话里通常只关心"最像的那个是谁"。
        """
        results: list[FaceMatch] = []
        for face in faces:
            m = self.match(face.feat)
            results.append(FaceMatch(name=m.name, similarity=m.similarity, box=face.box))
        results.sort(key=lambda m: m.similarity, reverse=True)
        return results

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def names(self) -> list[str]:
        """全部已登记的名字（按最近更新倒序）。"""
        with self._lock:
            return [
                name
                for name, _ in sorted(
                    self._people.items(), key=lambda kv: kv[1]["updated_at"], reverse=True
                )
            ]

    def count(self) -> int:
        """人数。"""
        with self._lock:
            return len(self._people)

    def sample_count(self) -> int:
        """特征总条数。"""
        with self._lock:
            return sum(len(entry["samples"]) for entry in self._people.values())

    def snapshot(self) -> dict[str, Any]:
        """导出给控制台看的概览（**不含**特征本身）。"""
        with self._lock:
            return {
                "enabled": self.enabled,
                "threshold": self.threshold,
                "count": len(self._people),
                "samples": sum(len(e["samples"]) for e in self._people.values()),
                "people": [self._summary(name, entry) for name, entry in self._sorted()],
            }

    def _sorted(self) -> list[tuple[str, dict[str, Any]]]:
        """按最近更新倒序（调用方需持锁）。"""
        return sorted(self._people.items(), key=lambda kv: kv[1]["updated_at"], reverse=True)

    def _summary(self, name: str, entry: dict[str, Any]) -> dict[str, Any]:
        """把一条登记记录压成不含特征的摘要。"""
        return {
            "name": name,
            "samples": len(entry["samples"]),
            "created_at": entry["created_at"],
            "updated_at": entry["updated_at"],
            "hits": entry.get("hits", 0),
            "source": entry.get("source", ""),
        }


# --------------------------------------------------------------------------- #
# 进程级单例（按路径区分）
# --------------------------------------------------------------------------- #
_dbs: dict[str, FaceDB] = {}
_db_lock = threading.Lock()


def get_face_db(
    path: str | Path, *, threshold: float = 0.5, max_samples: int = 8, enabled: bool = True
) -> FaceDB:
    """取得（或创建）指定路径的人脸库。

    与长期记忆一样用单例：Agent 会因配置热更新被重建，
    但人脸库必须保持同一份，否则刚绑定的名字会"消失"。
    """
    key = str(Path(path).resolve())
    with _db_lock:
        db = _dbs.get(key)
        if db is None or db.threshold != threshold or db.enabled != enabled:
            db = FaceDB(path, threshold=threshold, max_samples=max_samples, enabled=enabled)
            _dbs[key] = db
        return db
