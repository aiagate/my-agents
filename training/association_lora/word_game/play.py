import argparse
from pathlib import Path

from mlx_lm import generate, load
from mlx_lm.sample_utils import make_sampler

parser = argparse.ArgumentParser()
parser.add_argument("model", type=Path)
parser.add_argument("word")
args = parser.parse_args()
model = args.model
m, t = load(
    str(model), trust_remote_code=True, tokenizer_config={"trust_remote_code": True}
)
system = {
    "role": "system",
    "content": "日本語の連想ゲームです。お題から連想する単語を一語だけ返してください。",
}


def reply(word):
    p = t.apply_chat_template(
        [system, {"role": "user", "content": word}],
        tokenize=False,
        add_generation_prompt=True,
    )
    return generate(m, t, p, max_tokens=32, sampler=make_sampler(temp=0)).strip()


print(reply(args.word))
