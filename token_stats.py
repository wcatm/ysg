"""Claude Code 状态栏助手：实时显示当前会话 token 总用量与文本总长度

由 statusLine 配置调用（stdin 传入会话 JSON，含 transcript_path）。
每次读取会话记录文件统计；文件未变化时用缓存，避免重复解析。
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def fmt(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def stats(path):
    """返回 (token总数, 文本字符总数)"""
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    chars = 0
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = d.get("message") or {}
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for it in c:
                if isinstance(it, dict) and isinstance(it.get("text"), str):
                    chars += len(it["text"])
        u = m.get("usage") or {}
        for k, dst in (("input_tokens", "input"), ("output_tokens", "output"),
                       ("cache_read_input_tokens", "cache_read"),
                       ("cache_creation_input_tokens", "cache_write")):
            tokens[dst] += u.get(k, 0) or 0
    return sum(tokens.values()), chars


_cache = {}


def stats_cached(path):
    p = Path(path)
    key = (str(p), p.stat().st_mtime)
    if _cache.get("key") == key:
        return _cache["val"]
    val = stats(p)
    _cache["key"] = key
    _cache["val"] = val
    return val


def main():
    # 统计最近修改的会话记录（当前活跃会话始终是最新的；不读 stdin，
    # Windows 下子进程 stdin 可能永远等不到 EOF 而卡死）
    cands = sorted(Path.home().glob(".claude/projects/*/*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not cands:
        print("📊 暂无会话数据")
        return
    path = cands[0]
    try:
        total, chars = stats_cached(path)
    except Exception:
        print("📊 统计中…")
        return
    print(f"📊 token {fmt(total)} | 文本 {fmt(chars)} 字")


if __name__ == "__main__":
    main()
