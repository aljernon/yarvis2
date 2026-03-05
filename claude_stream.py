import os

import anthropic
import typer
from anthropic.types import MessageParam, TextBlockParam

from clam_ptb.clam_ptb.settings.main import (
    CLAUDE_MODEL_NAME,
    load_env,
)

app = typer.Typer()

PROMPTS: dict[str, str] = {}

PROMPTS["convert_to_first_person"] = """
Are you given a file with "Core Knowledge Principles" for a LLM. This is combination of instructions from the user and knowledge about the world and the user.
This is only one file from a set of files that represent the whole set of instructions.
You will see a filename to give you a hint about the specifics of this file.

I want you to rewrite it in a way that an agentic LLM would write for itself.
By agentic LLM I mean the entity that actively interacts with the world,
and records its own thoughts and obervations as well as preferances communicated to it by the user.
In simple terms "you" -> "I"

Some of the items are about the user, e.g., the location and the apps that the user uses. Please refer to them as "Anton's location" or "Anton uses this app". It's the name of the user.

No need to mechanically add "I" or "mine" everywhere. It's ok to keep some instuctions in explicit "you" form if this feels naturual.
Use present tense and first person. E.g., instead of saying "I'll document" say "I document". That's just what you do.

Instructions from %(file_name)s start now.
-------
%(content)s
"""


PROMPTS["grammar"] = """
Please fix typos and grammar in the text below.
-------
%(content)s
"""


# def top_level_async(async_f: Callable[..., Awaitable[Any]]):
#     @functools.wraps(async_f)
#     def sync_f(*args, **kwargs):
#         asyncio.run(async_f(*args, **kwargs))

#     return sync_f


@app.command()
def run(input_file_name: str, prompt_name: str):
    prompt_tpl = PROMPTS[prompt_name]

    with open(input_file_name) as stream:
        input_str = stream.read()
    prompt = prompt_tpl % dict(
        content=input_str, file_name=os.path.basename(input_file_name)
    )
    history: list[MessageParam] = [
        {"role": "user", "content": [TextBlockParam(type="text", text=prompt)]}
    ]
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=CLAUDE_MODEL_NAME,
        max_tokens=10000,
        messages=history,
    )

    response_text = "\n".join(
        getattr(x, "text", f"<block of type {type(x)}>") for x in response.content
    )
    print(response_text)


if __name__ == "__main__":
    load_env()
    app()
