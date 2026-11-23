import argparse
import json
import random
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("source", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
old = args.source
args.output.mkdir(parents=True, exist_ok=False)
seen = set()
for split in ["valid", "train"]:
    rows = []
    rand = random.Random(42)
    for line in (old / f"{split}.jsonl").read_text().splitlines():
        tree = json.loads(json.loads(line)["messages"][-1]["content"])
        for b in tree["branches"]:
            n = rand.choice(b["l2"])
            for a, z in [
                (b["l1"], n["term"]),
                (n["term"], rand.choice(n["l3"])["term"]),
            ]:
                if a == z or (a, z) in seen or any(c.isspace() for c in a + z):
                    continue
                seen.add((a, z))
                rows.append(
                    {
                        "messages": [
                            {
                                "role": "system",
                                "content": "日本語の連想ゲームです。お題から連想する単語を一語だけ返してください。",
                            },
                            {"role": "user", "content": a},
                            {"role": "assistant", "content": z},
                        ]
                    }
                )
    rand.shuffle(rows)
    (args.output / f"{split}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    )
    print(split, len(rows))
