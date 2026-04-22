"""
rag_engine.py  —  RAG (Retrieval-Augmented Generation) engine
==============================================================
Architecture
  Primary  : ChromaDB vector store  +  sentence-transformers embeddings
  Fallback : TF-IDF cosine similarity  (sklearn, zero extra installs)

The engine auto-detects which backend is available at import time.

Corpus (what gets indexed)
  • 34 company profiles  (industry, HQ, rating, stipend ranges)
  • 18 role-skill mappings  (role → required skills)
  • All job listings from WebCrawler/table.html  (company, role, location, date)

Public API
  engine = RAGEngine()
  docs   = engine.retrieve("Which companies pay above ₹50k?", k=5)
  answer = engine.answer("Which companies pay above ₹50k?", chat_history=[])

  # Low-level helpers
  engine.is_indexed()   → bool
  engine.backend_name   → "chromadb" | "tfidf" engine.doc_count      → int
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

# ── Try to import heavy dependencies; degrade gracefully ─────────────────────
try:
    from sentence_transformers import SentenceTransformer
    import chromadb
    _CHROMA_OK = True
except ImportError:
    _CHROMA_OK = False

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# ── Resolve project root regardless of where this file lives ─────────────────
_HERE  = Path(__file__).resolve().parent
_ROOT  = _HERE  # rag_engine.py lives in project root

# ── Ollama config ─────────────────────────────────────────────────────────────
OLLAMA_BASE   = "http://localhost:11434"
DEFAULT_MODEL = "phi3:mini" # ── ChromaDB persistence path ─────────────────────────────────────────────────
CHROMA_PATH   = str(_ROOT / ".rag_chroma_db")
TFIDF_CACHE   = str(_ROOT / ".rag_tfidf_cache.pkl")
COLLECTION    = "internship_rag" # ── Embedding model (lightweight, ~80 MB, downloads once) ────────────────────
EMBED_MODEL   = "all-MiniLM-L6-v2" # ═════════════════════════════════════════════════════════════════════════════
# CORPUS BUILDER
# ═════════════════════════════════════════════════════════════════════════════

def _make_id(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:16]


def build_corpus() -> list[dict]:
    """
Returns a list of document dicts:
      { "id": str, "text": str, "metadata": dict }

    Sources:
      1. COMPANY_DB  → one doc per company
      2. ROLE_SKILLS → one doc per role cluster
      3. table.html  → one doc per job listing row """
    docs: list[dict] = []

    # ── 1. Company profiles ───────────────────────────────────────────────────
    try:
        sys.path.insert(0, str(_ROOT))
        from company_intelligence import COMPANY_DB, _get_stipend

        for key, db in COMPANY_DB.items():
            display = db.get("display", key.title())
            industry = db.get("industry", "Technology")
            hq       = db.get("hq", "India")
            rating   = db.get("rating", 0)
            rating_s = db.get("rating_src", "AmbitionBox")
            employees = db.get("employees", "Not disclosed")
            founded   = db.get("founded", "—")

            # Build stipend summary
            smap = db.get("stipends", {})
            stip_lines = []
            for role_key, (lo, hi) in smap.items():
                if role_key != "default":
                    stip_lines.append(f"{role_key} intern: ₹{lo:,}–₹{hi:,}/mo")
            lo_d, hi_d = smap.get("default", (15000, 25000))
            stip_lines.append(f"general/default: ₹{lo_d:,}–₹{hi_d:,}/mo")
            stip_text = "; ".join(stip_lines)

            text = (
                f"Profile for {display}. Company name: {display}. " f"Industry: {industry}. Sector: {industry}. " f"Headquarters location: {hq}. " f"Founded: {founded}. Employee count: {employees}. " f"AmbitionBox / Glassdoor rating: {rating}/5 (source: {rating_s}). " f"Internship salary and stipend at {display} — {stip_text}. " f"Pay at {display}: {stip_text}. " f"This company {display} operates in {industry} sector " f"and is headquartered in {hq}, India." )

            docs.append({ "id":   _make_id(f"company:{key}"), "text": text, "metadata": { "type": "company_profile", "company":   display, "industry":  industry, "hq":        hq, "rating":    str(rating), "employees": employees, "stipend_default": f"₹{lo_d:,}–₹{hi_d:,}/mo",
                }
            })
    except Exception as e:
        print(f"[RAG] Warning: could not load COMPANY_DB — {e}")

    # ── 2. Role → Skills mappings ─────────────────────────────────────────────
    try:
        from company_intelligence import ROLE_SKILLS, DEFAULT_SKILLS

        for keywords, skills in ROLE_SKILLS:
            role_label = keywords[0].title()
            skill_text = ", ".join(skills)
            text = (
                f"Role: {role_label}. " f"Also known as: {', '.join(keywords)}. " f"Required skills and technologies: {skill_text}. " f"To get a {role_label} internship you need: {skill_text}." )
            docs.append({ "id":   _make_id(f"role:{keywords[0]}"), "text": text, "metadata": { "type": "role_skills", "role":   role_label, "skills": skill_text,
                }
            })

        # Default skills doc
        ds = ", ".join(DEFAULT_SKILLS)
        docs.append({ "id":   _make_id("role:default"), "text": (f"General internship skills required for any tech role: {ds}. " f"Every intern candidate should have: {ds}."), "metadata": {"type": "role_skills", "role": "General Tech Intern", "skills": ds}
        })
    except Exception as e:
        print(f"[RAG] Warning: could not load ROLE_SKILLS — {e}")

    # ── 3. Job listings from table.html ──────────────────────────────────────
    try:
        html_path = _ROOT / "WebCrawler" / "table.html"
        if html_path.exists():
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
            table = soup.find("table")
            if table:
                last_company = ""
                for tr in table.find_all("tr"):
                    tds = tr.find_all("td")
                    if not tds or len(tds) < 2:
                        continue
                    row = [
                        td.text.strip() if td.text.strip()
                        else (td.find("a").get("href") if td.find("a") else "")
                        for td in tds
                    ]
                    company = row[0].strip()
                    if company in ("↳", ""):
                        company = last_company
                    else:
                        last_company = company

                    role     = row[1] if len(row) > 1 else ""
                    location = row[2] if len(row) > 2 else "India"
                    link     = row[3] if len(row) > 3 else ""
                    date     = row[4] if len(row) > 4 else ""
                    if not role:
                        continue

                    text = (
                        f"Job listing: {company} is hiring for {role} " f"in {location or 'India'}. " f"Posted: {date or 'recently'}. " f"Apply at: {link or 'company website'}. " f"This is a {role} position at {company} located in {location}." )
                    docs.append({ "id":   _make_id(f"job:{company}:{role}:{date}"), "text": text, "metadata": { "type": "job_listing", "company":  company, "role":     role, "location": location or "India", "date":     date or "", "link":     link or "",
                        }
                    })
    except Exception as e:
        print(f"[RAG] Warning: could not load table.html — {e}")

    # ── 4. Synthetic FAQ docs (common queries answered directly) ─────────────
    faq_docs = [
        { "text": ( "Highest paying internships ranked by stipend salary: " "1. Google India pays ₹1,00,000–₹1,50,000 per month — top salary. " "2. Atlassian India pays ₹90,000–₹1,30,000 per month. " "3. Microsoft India pays ₹85,000–₹1,20,000 per month. " "4. Amazon India pays ₹80,000–₹1,20,000 per month. " "5. Flipkart pays ₹70,000–₹1,00,000 per month. " "6. LinkedIn India pays ₹50,000–₹80,000 per month. " "7. Visa India pays ₹60,000–₹95,000 per month. " "8. Uber India pays ₹60,000–₹90,000 per month. " "9. Spotify India pays ₹60,000–₹90,000 per month. " "10. Zomato pays ₹40,000–₹72,000 per month. " "Best salary internships are at product companies. " "Maximum stipend internship: Google India. " "Top salary paying companies for interns in India. " "Highest compensation for software engineering interns." ), "metadata": {"type": "faq", "topic": "highest_paying"}
        },
        { "text": ( "Companies with the best ratings (above 4.0) are: " "Google India (4.5), Atlassian India (4.5), Spotify India (4.5), " "LinkedIn India (4.3), Visa India (4.3), Microsoft India (4.4), " "Accenture India (4.0), Amazon India (4.0), Deloitte India (4.1), " "Oracle India (4.1). " "Ratings are sourced from AmbitionBox and Glassdoor India." ), "metadata": {"type": "faq", "topic": "best_rated"}
        },
        { "text": ( "For fresher / entry-level software internships the most important skills are: " "Data Structures & Algorithms (DSA), Python or Java, " "Git & version control, SQL basics, Object-Oriented Programming, " "REST API concepts, and strong problem-solving ability. " "Most Indian tech companies test DSA in their intern hiring process." ), "metadata": {"type": "faq", "topic": "fresher_skills"}
        },
        { "text": ( "IT services companies (TCS, Infosys, Wipro, Capgemini, Cognizant, " "Tech Mahindra, HCL, CGI) typically offer stipends between ₹14,000 and ₹30,000/mo. " "Product companies (Google, Microsoft, Amazon, Flipkart, Atlassian) offer " "significantly higher stipends ranging from ₹50,000 to ₹1,50,000/mo. " "Consulting firms (EY, Deloitte, PwC) offer ₹20,000–₹48,000/mo." ), "metadata": {"type": "faq", "topic": "stipend_comparison"}
        },
        { "text": ( "Internship locations in India: " "Bengaluru (Bangalore) Karnataka has the most internship opportunities. " "Companies in Bengaluru: Google India, Infosys, Flipkart, Swiggy, " "Atlassian India, Uber India, LinkedIn India, Glassdoor India, " "HackerEarth, Ola Electric, BYJU'S, Accenture India, Oracle India, " "IBM India, CGI India, EY India, Visa India, Spotify India, Paytm (Noida). " "Companies in Hyderabad: Microsoft India, Indeed India, Phenom. " "Companies in Mumbai: TCS, Deloitte India, PwC India, Capgemini India. " "Companies in Gurugram: Internshala, Zomato. " "Companies in Noida: Naukri.com, HCL Technologies. " "Companies in Pune: Tech Mahindra. " "Best cities for internships in India: Bengaluru, Hyderabad, Mumbai, Pune, Delhi NCR." ), "metadata": {"type": "faq", "topic": "locations"}
        },
        { "text": ( "For a Machine Learning or Data Science internship you need: " "Python (TensorFlow, PyTorch, scikit-learn), Deep Learning (CNNs, RNNs, Transformers), " "Mathematics (Linear Algebra, Calculus, Probability & Statistics), " "pandas and numpy for data wrangling, " "SQL for data querying, " "NLP or Computer Vision basics. " "Companies hiring ML interns include Amazon India, Google India, IBM India, " "Microsoft India, Flipkart, and Paytm." ), "metadata": {"type": "faq", "topic": "ml_skills"}
        },
    ]
    for faq in faq_docs:
        docs.append({ "id":       _make_id(f"faq:{faq['metadata']['topic']}"), "text":     faq["text"], "metadata": faq["metadata"],
        })

    print(f"[RAG] Corpus built: {len(docs)} documents")
    return docs


# ═════════════════════════════════════════════════════════════════════════════
# RAG ENGINE
# ═════════════════════════════════════════════════════════════════════════════

class RAGEngine:
    """
Unified RAG engine with ChromaDB (semantic) or TF-IDF (fallback) backends.
    Thread-safe for single-process Streamlit use.
    """
    def __init__(self):
        self._docs:      list[dict] = []
        self._chroma     = None
        self._collection = None
        self._embed_fn   = None
        self._tfidf_vec  = None
        self._tfidf_mat  = None
        self._tfidf_meta: list[dict] = []

        if _CHROMA_OK:
            self._init_chromadb()
            self.backend_name = "chromadb"
        else:
            self._init_tfidf()
            self.backend_name = "tfidf"

    # ── Backend initialisation ────────────────────────────────────────────────

    def _init_chromadb(self):
        """Set up ChromaDB + sentence-transformers."""
        try:
            self._embed_model = SentenceTransformer(EMBED_MODEL)
            self._chroma = chromadb.PersistentClient(path=CHROMA_PATH)
            # Try getting existing collection; reset if corrupted
            try:
                self._collection = self._chroma.get_collection(COLLECTION)
            except Exception:
                self._collection = self._chroma.create_collection(
                    COLLECTION,
                    metadata={"hnsw:space": "cosine"}
                )
            print(f"[RAG] ChromaDB backend ready. Collection has {self._collection.count()} docs.")
        except Exception as e:
            print(f"[RAG] ChromaDB init failed: {e}. Falling back to TF-IDF.")
            self._chroma = None
            self._collection = None
            self._init_tfidf()
            self.backend_name = "tfidf"

    def _init_tfidf(self):
        """Set up TF-IDF backend (no extra packages needed)."""
        if Path(TFIDF_CACHE).exists():
            try:
                with open(TFIDF_CACHE, "rb") as f:
                    cache = pickle.load(f)
                self._tfidf_vec  = cache["vec"]
                self._tfidf_mat  = cache["mat"]
                self._tfidf_meta = cache["meta"]
                self._docs       = cache["docs"]
                print(f"[RAG] TF-IDF backend loaded from cache. {len(self._docs)} docs.")
                return
            except Exception:
                pass  # cache corrupt — rebuild below
        print("[RAG] TF-IDF backend initialised (index not built yet).")

    # ── Indexing ──────────────────────────────────────────────────────────────

    def is_indexed(self) -> bool:
        if self.backend_name == "chromadb" and self._collection:
            return self._collection.count() > 0
        return len(self._docs) > 0

    @property
    def doc_count(self) -> int:
        if self.backend_name == "chromadb" and self._collection:
            return self._collection.count()
        return len(self._docs)

    def build_index(self, progress_cb=None) -> int:
        """Build the full index from the corpus.
        progress_cb(pct: int, msg: str) called periodically for UI updates.
        Returns number of documents indexed."""
        if progress_cb:
            progress_cb(5, "Building corpus from company data…")

        corpus = build_corpus()
        self._docs = corpus

        if not corpus:
            return 0

        if self.backend_name == "chromadb" and self._collection is not None:
            return self._index_chromadb(corpus, progress_cb)
        else:
            return self._index_tfidf(corpus, progress_cb)

    def _index_chromadb(self, corpus: list[dict], progress_cb=None) -> int:
        """Index corpus into ChromaDB with sentence-transformer embeddings."""
        if progress_cb:
            progress_cb(20, f"Embedding {len(corpus)} documents with {EMBED_MODEL}…")

        # Reset collection to avoid duplicates
        try:
            self._chroma.delete_collection(COLLECTION)
        except Exception:
            pass
        self._collection = self._chroma.create_collection(
            COLLECTION,
            metadata={"hnsw:space": "cosine"}
        )

        texts = [d["text"]     for d in corpus]
        ids   = [d["id"]       for d in corpus]
        metas = [d["metadata"] for d in corpus]

        # Embed in batches of 64
        batch_size = 64
        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            embs  = self._embed_model.encode(batch, show_progress_bar=False).tolist()
            all_embeddings.extend(embs)
            pct = 20 + int(70 * (i + len(batch)) / len(texts))
            if progress_cb:
                progress_cb(pct, f"Embedding… {min(i+batch_size, len(texts))}/{len(texts)}")

        self._collection.add(
            embeddings=all_embeddings,
            documents=texts,
            ids=ids,
            metadatas=metas,
        )

        if progress_cb:
            progress_cb(100, f"Indexed {len(corpus)} documents ")

        print(f"[RAG] ChromaDB indexed {len(corpus)} documents.")
        return len(corpus)

    def _index_tfidf(self, corpus: list[dict], progress_cb=None) -> int:
        """Index corpus with TF-IDF (instant, no embeddings)."""
        if progress_cb:
            progress_cb(30, "Building TF-IDF index…")

        texts = [d["text"] for d in corpus]
        self._tfidf_vec  = TfidfVectorizer(
            ngram_range=(1, 2),
            max_features=20000,
            stop_words="english",
        )
        self._tfidf_mat  = self._tfidf_vec.fit_transform(texts)
        self._tfidf_meta = [d["metadata"] for d in corpus]

        # Persist cache
        try:
            with open(TFIDF_CACHE, "wb") as f:
                pickle.dump({ "vec":  self._tfidf_vec, "mat":  self._tfidf_mat, "meta": self._tfidf_meta, "docs": corpus,
                }, f)
        except Exception:
            pass

        if progress_cb:
            progress_cb(100, f"Indexed {len(corpus)} documents ")

        print(f"[RAG] TF-IDF indexed {len(corpus)} documents.")
        return len(corpus)

    def reset_index(self):
        """
Wipe and rebuild the index from scratch."""
        # Delete ChromaDB
        if self.backend_name == "chromadb" and self._chroma:
            try:
                self._chroma.delete_collection(COLLECTION)
                self._collection = self._chroma.create_collection(
                    COLLECTION, metadata={"hnsw:space": "cosine"}
                )
            except Exception:
                pass
        # Delete TF-IDF cache
        if Path(TFIDF_CACHE).exists():
            Path(TFIDF_CACHE).unlink()
        self._docs       = []
        self._tfidf_vec  = None
        self._tfidf_mat  = None
        self._tfidf_meta = []
        print("[RAG] Index reset.")

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def retrieve(self, query: str, k: int = 5) -> list[dict]:
        """Return top-k most relevant documents for the query.
        Each result: { "text": str, "metadata": dict, "score": float }"""
        if not self.is_indexed():
            return []

        if self.backend_name == "chromadb" and self._collection and self._embed_model:
            return self._retrieve_chromadb(query, k)
        return self._retrieve_tfidf(query, k)

    def _retrieve_chromadb(self, query: str, k: int) -> list[dict]:
        q_emb = self._embed_model.encode([query]).tolist()
        results = self._collection.query(
            query_embeddings=q_emb,
            n_results=min(k, self._collection.count()),
            include=["documents", "metadatas", "distances"],
        )
        out = []
        docs  = results.get("documents", [[]])[0]
        metas = results.get("metadatas",  [[]])[0]
        dists = results.get("distances",  [[]])[0]
        for text, meta, dist in zip(docs, metas, dists):
            score = max(0.0, 1.0 - dist)   # cosine distance → similarity
            out.append({"text": text, "metadata": meta, "score": round(score, 3)})
        return out

    def _retrieve_tfidf(self, query: str, k: int) -> list[dict]:
        q_vec = self._tfidf_vec.transform([query])
        sims  = cosine_similarity(q_vec, self._tfidf_mat)[0]
        top_k = np.argsort(sims)[::-1][:k]
        out   = []
        for idx in top_k:
            if sims[idx] < 0.01:
                continue
            out.append({ "text":     self._docs[idx]["text"], "metadata": self._tfidf_meta[idx], "score":    round(float(sims[idx]), 3),
            })
        return out

    # ── Generation ────────────────────────────────────────────────────────────

    def _get_ollama_model(self) -> str:
        """
Get currently selected Ollama model from session state if available."""
        try:
            import streamlit as st
            return st.session_state.get( "ollama_model_pick",
                st.session_state.get("ollama_model_manual", DEFAULT_MODEL)
            )
        except Exception:
            return DEFAULT_MODEL

    def ping_ollama(self) -> tuple[bool, str]:
        """
        Fast connectivity check — does NOT load the model.
        Returns (ok: bool, error_msg: str).
        Uses /api/tags (instant, no model load needed).
        """
        try:
            req = urllib.request.Request(
                f"{OLLAMA_BASE}/api/tags", method="GET"
            )
            with urllib.request.urlopen(req, timeout=4) as r:
                data   = json.loads(r.read().decode())
                models = [m["name"] for m in data.get("models", [])]
                return True, f"Ollama running. Models: {models}"
        except Exception as e:
            return False, str(e)

    def warmup_model(self) -> tuple[bool, str]:
        """Pre-load phi3:mini into VRAM with a 1-token request."""
        model = self._get_ollama_model()
        payload = json.dumps({
            "model":  model,
            "prompt": "Hi",
            "stream": False,
            "options": {"num_predict": 1},
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_BASE}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                json.loads(r.read().decode())
            return True, ""
        except Exception as e:
            return False, str(e)

    def _ollama_generate(self, prompt: str, timeout: int = 180) -> tuple[str, str | None]:
        """Call /api/generate — faster than /api/chat for RAG."""
        model = self._get_ollama_model()
        payload = json.dumps({
            "model":  model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature":    0.25,
                "num_predict":    400,
                "top_p":          0.85,
                "repeat_penalty": 1.1,
            },
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_BASE}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
                text = data.get("response", "").strip()
                if not text:
                    return "", "Model returned empty response"
                return text, None
        except urllib.error.URLError as e:
            reason = str(e)
            if "timed out" in reason or "timeout" in reason.lower():
                return "", (
                    "Request timed out. phi3:mini may still be loading into memory. "
                    "Please wait 10-15 seconds and try again. "
                    "This only happens on the first message after restarting Ollama."
                )
            return "", f"Cannot reach Ollama ({reason}). Run: ollama serve"
        except Exception as e:
            return "", f"Ollama error: {e}"

    def _quick_answer(self, query: str, docs: list[dict]) -> str | None:
        """For high-confidence single-doc queries, answer directly from retrieval."""
        ql = query.lower()
        import re as _re

        if docs and docs[0]["metadata"].get("type") == "company_profile" and docs[0]["score"] > 0.25:
            d = docs[0]
            m = d["metadata"]
            stip_match = _re.search(r"general/default: (₹[\d,]+–₹[\d,]+/mo)", d["text"])
            stip     = stip_match.group(1) if stip_match else m.get("stipend_default", "—")
            company  = m.get("company", "?")
            industry = m.get("industry", "?")
            hq       = m.get("hq", "?")
            rating   = m.get("rating", "?")
            if any(kw in ql for kw in ["stipend","salary","pay","how much","package","compensation"]):
                return (
                    f"**{company}** internship stipend:\n\n"
                    f"- Default/general roles: **{stip}**\n"
                    f"- Industry: {industry}\n"
                    f"- Location: {hq}\n"
                    f"- Rating: {rating}/5"
                )
            if any(kw in ql for kw in ["about","tell me","what is","detail","info","profile"]):
                return (
                    f"**{company}**\n\n"
                    f"- Industry: {industry}\n"
                    f"- HQ: {hq}\n"
                    f"- Rating: {rating}/5\n"
                    f"- Typical intern stipend: {stip}"
                )

        skill_kws = ["skill","need","require","learn","know","prepare","what do i need","how to"]
        for d in docs[:2]:
            if d["metadata"].get("type") == "role_skills" and d["score"] > 0.14:
                role = d["metadata"].get("role", "this role")
                skls = d["metadata"].get("skills", "")
                if skls and any(kw in ql for kw in skill_kws):
                    items = [s.strip() for s in skls.split(",") if s.strip()]
                    return f"**Skills needed for {role}:**\n\n" + "\n".join(f"- {s}" for s in items)

        for d in docs[:2]:
            if d["metadata"].get("type") == "faq" and d["score"] > 0.09:
                topic = d["metadata"].get("topic", "")
                if topic in ("ml_skills", "fresher_skills") and any(
                    kw in ql for kw in skill_kws + ["ml","machine learning","data science","fresher"]
                ):
                    txt   = d["text"]
                    parts = txt.split("you need: ", 1)
                    if len(parts) == 2:
                        skills_raw = parts[1].split(". Companies hiring")[0]
                        items = [s.strip() for s in skills_raw.split(",") if s.strip()]
                        label = "Machine Learning / Data Science" if "ml" in topic else "General Tech"
                        return f"**Skills needed for {label} internship:**\n\n" + "\n".join(f"- {s}" for s in items)

        return None

    def answer(self, query: str, chat_history: list[dict], k: int = 5) -> tuple[str, list[dict], str | None]:
        """Full RAG pipeline. Returns (answer_text, retrieved_docs, error_or_None)."""
        if not self.is_indexed():
            return ("Knowledge base not indexed yet. Please send a message to trigger indexing.", [], None)

        retrieved = self.retrieve(query, k=k)
        quick = self._quick_answer(query, retrieved)
        if quick:
            return quick, retrieved, None

        ctx_parts = []
        for i, doc in enumerate(retrieved[:3], 1):
            txt = doc["text"][:220].rstrip()
            ctx_parts.append(f"[{i}] {txt}")
        context = "\n".join(ctx_parts) if ctx_parts else "No data found."

        prev = ""
        if chat_history:
            last_pair = chat_history[-2:] if len(chat_history) >= 2 else chat_history
            for m in last_pair:
                role = "User" if m["role"] == "user" else "Assistant"
                prev += f"{role}: {m['content'][:120]}\n"

        prompt = (
            "You are a helpful Indian internship advisor.\n"
            "Answer the question using ONLY the facts below. "
            "Be specific, use bullet points for lists, "
            "quote exact rupee amounts.\n\n"
            f"FACTS:\n{context}\n\n"
            + (f"RECENT CHAT:\n{prev}\n" if prev else "")
            + f"QUESTION: {query}\n\nANSWER:"
        )

        response, err = self._ollama_generate(prompt)
        if err:
            return "", retrieved, err

        response = response.lstrip("ANSWER:").lstrip(":").strip()
        return response, retrieved, None


# ═════════════════════════════════════════════════════════════════════════════
# SINGLETON  (one engine per process — expensive to reload)
# ═════════════════════════════════════════════════════════════════════════════

_engine_instance: RAGEngine | None = None

def get_engine() -> RAGEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = RAGEngine()
    return _engine_instance


# ═════════════════════════════════════════════════════════════════════════════
# QUICK SANITY CHECK  (run: python rag_engine.py)
# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("Testing RAG engine...")
    engine = get_engine()
    print(f"Backend: {engine.backend_name}")

    if not engine.is_indexed():
        print("Building index...")
        n = engine.build_index(lambda pct, msg: print(f" [{pct}%] {msg}"))
        print(f"Indexed {n} documents")
    else:
        print(f"Index already built: {engine.doc_count} docs")

    # Test retrieval
    test_queries = [ "Which company pays the highest stipend?", "What skills do I need for a data science internship?", "Tell me about Google India internship",
    ]
    for q in test_queries:
        print(f"\nQ: {q}")
        docs = engine.retrieve(q, k=3)
        for d in docs:
            print(f" [{d['score']:.2f}] {d['metadata'].get('company','?')} | {d['text'][:80]}…")