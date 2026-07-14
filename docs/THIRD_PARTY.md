# Third-party components and credits

External runtimes Nova adapts or shells out to. **DriveAuth Edge is not third-party** — it is the core payment gate (see [pipeline.md](pipeline.md#driveauth-two-gates) and [README Credits](../README.md#credits)).

## speech-to-speech (adapted runtime)

- Upstream: [huggingface/speech-to-speech](https://github.com/huggingface/speech-to-speech)
- Submodule: `cloned/speech-to-speech`
- Adapted in-tree for Nova route/agent/DriveAuth hooks

## llama.cpp

- Build `llama-server` locally; set `LLAMA_SERVER_BIN` in `.env`
- Do not commit the binary or GGUF weights
