"""CLI 版排行采集: python scripts/rank_collect.py [页数] (Web 控制台可代替)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sj_bot.collector import RankCollector


def main() -> int:
    pages = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    RankCollector(pages=pages, log=lambda lv, m: print(f"[{lv}] {m}", flush=True)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
