# Choosing & installing the lyric LLM

Music World talks to **llama.cpp**, **Ollama**, or any **OpenAI-compatible**
endpoint (set in Admin). For lyric writing you want strong instruction-following
*and* clean JSON. On a **Strix Halo** box (Ryzen AI Max+, ~128 GB unified
memory) two picks stand out:

| Pick | Why | Size (Q5_K_M) |
|------|-----|---------------|
| **Qwen3‑30B‑A3B Instruct** (MoE) | Only ~3 B params active → fast on Strix Halo's memory bandwidth; great instructions + JSON. Use the **Instruct** (non‑thinking) build so it doesn't emit `<think>`. | ~21 GB |
| **Hermes 4.3 36B** (Nous Research) | Best constraint‑following + steerable + low‑refusal of the families you asked about; dense, so slower but high quality. | ~25 GB |

With 128 GB you can comfortably run **Q6_K or Q8_0** of either for a quality bump.
MoE (Qwen3) is the better choice for batch work like *brief‑all / render‑all*.

Model pages:
- Qwen3‑30B‑A3B: [Qwen/Qwen3-30B-A3B-GGUF](https://huggingface.co/Qwen/Qwen3-30B-A3B-GGUF) ·
  non‑thinking [Qwen/Qwen3-30B-A3B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507) ·
  [unsloth GGUF](https://huggingface.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF)
- Hermes 4.3 36B: [NousResearch/Hermes-4.3-36B-GGUF](https://huggingface.co/NousResearch/Hermes-4.3-36B-GGUF) ·
  [bartowski GGUF](https://huggingface.co/bartowski/NousResearch_Hermes-4.3-36B-GGUF) ·
  [Hermes 4 collection](https://huggingface.co/collections/NousResearch/hermes-4-collection)

---

## Option A — Ollama (easiest)

Install Ollama (<https://ollama.com/download>), then pull a model. Any HF GGUF can
be pulled directly with the `hf.co/...` form:

```bash
# Qwen3 MoE (fast), non-thinking instruct, Q5_K_M:
ollama pull hf.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF:Q5_K_M

# Hermes 4.3 36B, Q5_K_M:
ollama pull hf.co/NousResearch/Hermes-4.3-36B-GGUF:Q5_K_M

ollama serve   # if not already running (listens on :11434)
```

In **Music World → Admin → Language model**:
- Backend: **Ollama**
- Base URL: `http://localhost:11434`
- Model: the exact tag you pulled, e.g. `hf.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF:Q5_K_M`
- **Test connection**, then **Save settings**.

---

## Option B — llama.cpp (most control on Strix Halo)

Build llama.cpp with the **Vulkan** backend (the most reliable path on Strix
Halo's iGPU today; ROCm/gfx1151 also works on recent builds):

```bash
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
cmake -B build -DGGML_VULKAN=ON && cmake --build build -j   # or -DGGML_HIP=ON for ROCm
```

Download a GGUF (pick one quant; Q6_K is a good 128 GB default):

```bash
pip install -U "huggingface_hub[cli]"
huggingface-cli download unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF \
  --include "*Q6_K*" --local-dir ./models/qwen3-30b-a3b
# or Hermes:
huggingface-cli download bartowski/NousResearch_Hermes-4.3-36B-GGUF \
  --include "*Q6_K*" --local-dir ./models/hermes-4.3-36b
```

Serve it (OpenAI-compatible API on :8080; `-ngl 99` offloads all layers to the
iGPU via unified memory):

```bash
./build/bin/llama-server -m ./models/qwen3-30b-a3b/*Q6_K*.gguf \
  -c 8192 -ngl 99 --host 0.0.0.0 --port 8080
```

In **Admin → Language model**:
- Backend: **llama.cpp server (OpenAI-compatible)**
- Base URL: `http://localhost:8080`
- Model: leave blank (llama.cpp serves whatever is loaded)
- **Test connection**, **Save settings**.

---

## Settings that matter

- **Temperature ≈ 0.7** — used for the structured brief (keeps JSON clean).
- **Lyric temperature ≈ 0.95** — the lyrics are written in a *separate* pass, so
  you can run them hotter for creative words without breaking JSON.
- **Max tokens ≥ 4096** — a brief + lyrics is large; truncation looks like short
  or broken lyrics.
- **Turn off "thinking."** The app already requests it, but the cleanest path is
  a **non-thinking** build (Qwen3 *Instruct‑2507*); for hybrid models you can also
  append `/no_think` to prompts or disable reasoning in the model's options.

See the **Lyric style / structure / per-genre idiom** controls in Admin and on the
track page for steering *what* the lyrics say (separate from which model writes them).
