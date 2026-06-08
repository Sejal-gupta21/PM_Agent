#!/usr/bin/env python3
"""Build embeddings for functionality documents and save to outputs.

Usage:
  PYTHONPATH=. python3 scripts/build_embeddings.py

This script will attempt to use OpenAI if `OPENAI_API_KEY` is present in env.
It loads `outputs/functionality_docs.json` and writes `outputs/functionality_embeddings.json`.
If OpenAI is not available, it will write a metadata-only placeholder file.
"""
import os
import json
import math
from pathlib import Path
from config import config

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
DOCS_PATH = OUT / "functionality_docs.json"
EMB_OUT = OUT / "functionality_embeddings.json"


def main():
    if not DOCS_PATH.exists():
        print(f"No functionality docs found at {DOCS_PATH}. Run functionality mapping first.")
        return

    docs_raw = json.loads(DOCS_PATH.read_text())
    # functionality_docs.json may be a dict mapping path->meta or a list of docs
    if isinstance(docs_raw, dict):
        docs = []
        for k, v in docs_raw.items():
            item = dict(v)
            item.setdefault("id", k)
            docs.append(item)
    else:
        docs = docs_raw

    # Prepare outputs dir
    OUT.mkdir(parents=True, exist_ok=True)

    openai_key = config.openai_api_key

    embeddings = []

    if openai_key:
        try:
            import openai

            openai.api_key = openai_key
            model = config.openai_embedding_model
            print("Using OpenAI to create embeddings (model=%s)" % model)
            for doc in docs:
                text = doc.get("text") or doc.get("summary") or doc.get("content") or doc.get("path") or doc.get("heuristic_label") or ""
                if not text:
                    text = doc.get("path", "")
                # simple safeguard to limit length
                if len(text) > 5000:
                    text = text[:5000]
                resp = openai.Embedding.create(model=model, input=text)
                emb = resp["data"][0]["embedding"]
                embeddings.append({"id": doc.get("id") or doc.get("path"), "embedding": emb, "meta": doc})
        except Exception as e:
            print("Failed to import or call OpenAI:", e)
            openai_key = None

    if not openai_key:
        # No OpenAI key available — write metadata-only file and exit with instructions.
        print("OPENAI_API_KEY not found or failed. Wrote metadata-only embeddings file.")
        for i, doc in enumerate(docs):
            embeddings.append({"id": doc.get("id") or doc.get("path") or f"doc-{i}", "embedding": None, "meta": doc})

    EMB_OUT.write_text(json.dumps(embeddings, indent=2))
    print(f"Wrote embeddings (or placeholders) to {EMB_OUT}")


if __name__ == "__main__":
    main()
