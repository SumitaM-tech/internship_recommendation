"""
pages/2_Career_Chatbot.py
Career assistant powered by RAG — professional, emoji-free UI.
"""

import os
import sys
import time

import streamlit as st

_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _root not in sys.path:
    sys.path.insert(0, _root)

_RAG_AVAILABLE = False
_rag_err_msg   = ""
try:
    from rag_engine import get_engine, _CHROMA_OK
    _RAG_AVAILABLE = True
except Exception as _e:
    _rag_err_msg = str(_e)

st.set_page_config(
    page_title="Career Chatbot — RAG",
    page_icon="C",
    layout="wide",
)

# ══════════════════════════════════════════════════════════════════════════════
# CSS
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("""<style>
[data-testid="stSidebar"] { background: rgba(4,10,28,0.98); }
.block-container          { padding-top: 1.2rem; }

/* ── Message bubbles ── */
.bubble-user {
    background: linear-gradient(135deg,#1a3a6a,#0f2244);
    border: 1px solid rgba(74,144,217,0.4);
    border-radius: 18px 18px 4px 18px;
    padding: 12px 18px;
    margin: 8px 0 8px 80px;
    color: #e8eef8;
    font-size: 0.88rem;
    line-height: 1.6;
}
.bubble-bot {
    background: rgba(8,18,48,0.85);
    border: 1px solid rgba(74,144,217,0.16);
    border-radius: 18px 18px 18px 4px;
    padding: 14px 20px;
    margin: 8px 80px 8px 0;
    color: #d8e8f8;
    font-size: 0.88rem;
    line-height: 1.75;
}
.bubble-quick {
    background: rgba(15,40,30,0.8);
    border: 1px solid rgba(34,197,94,0.22);
    border-radius: 18px 18px 18px 4px;
    padding: 14px 20px;
    margin: 8px 80px 8px 0;
    color: #d0ecd8;
    font-size: 0.88rem;
    line-height: 1.75;
}
.blabel {
    font-size: 0.61rem;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 6px;
    display: block;
    color: #4a90d9;
}
.bubble-user .blabel  { color: #7ec8f7; }
.bubble-quick .blabel { color: #34d399; }

/* ── Source strip ── */
.src-strip {
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid rgba(74,144,217,0.1);
}
.src-lbl  { font-size: 0.6rem; color: #3a5a7a; font-weight: 700;
            text-transform: uppercase; letter-spacing: 0.5px; margin-right: 6px; }
.src-chip {
    display: inline-block;
    background: rgba(74,144,217,0.07);
    border: 1px solid rgba(74,144,217,0.22);
    border-radius: 20px;
    padding: 2px 10px;
    font-size: 0.64rem;
    font-weight: 700;
    color: #7ec8f7;
    margin: 2px 3px 2px 0;
}
.instant-badge {
    display: inline-block;
    background: rgba(34,197,94,0.1);
    border: 1px solid rgba(34,197,94,0.3);
    border-radius: 20px;
    padding: 1px 8px;
    font-size: 0.6rem;
    font-weight: 700;
    color: #22c55e;
    margin-left: 6px;
    vertical-align: middle;
}

/* ── Input ── */
div[data-testid="stTextInput"] input {
    background: rgba(8,18,50,0.8) !important;
    border: 1px solid rgba(74,144,217,0.38) !important;
    color: #e8eef8 !important;
    border-radius: 10px !important;
    font-size: 0.88rem !important;
}
div[data-testid="stTextInput"] input:focus {
    border-color: #4a90d9 !important;
    box-shadow: 0 0 0 2px rgba(74,144,217,0.12) !important;
}

/* ── Sidebar badges ── */
.sb-badge {
    border-radius: 9px; padding: 8px 12px;
    margin-bottom: 10px; font-size: 0.7rem; font-weight: 700;
}
</style>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# SESSION STATE
# ══════════════════════════════════════════════════════════════════════════════
_DEFS = {
    "rag_messages":  [],
    "rag_indexed":   False,
    "rag_fkey":      0,
    "rag_pending":   "",
    "rag_ollama_ok": None,
    "rag_warmed":    False,
}
for _k, _v in _DEFS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════
SUGGESTIONS = [
    "Which internship pays the highest stipend?",
    "Tell me about Google India",
    "Skills needed for ML internship?",
    "Compare TCS, Infosys and Wipro",
    "Which companies are in Bengaluru?",
    "Best rated companies to intern at?",
    "Skills for full stack developer?",
    "Flipkart vs Amazon — SDE intern?",
    "How much does Microsoft India pay?",
    "Consulting internships — EY and Deloitte?",
    "I'm a fresher, what to focus on?",
    "IT vs product companies difference?",
]

TYPE_LABEL = {
    "company_profile": "Company",
    "role_skills":     "Role Skills",
    "job_listing":     "Job Listing",
    "faq":             "FAQ",
}

FOLLOWUP_MAP = {
    "company_profile": [
        "What skills do I need for this company?",
        "How does this compare to similar companies?",
    ],
    "role_skills": [
        "Which companies hire for this role?",
        "What is the average stipend for this role?",
    ],
    "faq": [
        "Which companies are best for a fresher?",
        "What should I focus on first?",
    ],
}

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _src_html(sources: list) -> str:
    if not sources:
        return ""
    chips, seen = [], set()
    for s in sources:
        m   = s.get("metadata", {})
        typ = m.get("type", "")
        pct = round(s.get("score", 0) * 100)
        lbl = TYPE_LABEL.get(typ, typ)
        if typ == "company_profile":
            lbl = m.get("company", lbl)
        elif typ == "role_skills":
            lbl = m.get("role", lbl)
        elif typ == "faq":
            lbl = m.get("topic", typ).replace("_", " ").title()
        key = f"{typ}:{lbl}"
        if key not in seen:
            seen.add(key)
            chips.append(f"<span class='src-chip'>{lbl} {pct}%</span>")
    return (
        "<div class='src-strip'>"
        "<span class='src-lbl'>Sources</span>"
        + "".join(chips)
        + "</div>"
    )


def _render_chat():
    for idx, msg in enumerate(st.session_state["rag_messages"]):
        role     = msg["role"]
        content  = msg["content"]
        sources  = msg.get("sources", [])
        is_quick = msg.get("is_quick", False)

        if role == "user":
            st.markdown(
                f"<div class='bubble-user'>"
                f"<span class='blabel'>You</span>"
                f"{content}</div>",
                unsafe_allow_html=True,
            )
        else:
            badge      = "<span class='instant-badge'>Instant</span>" if is_quick else ""
            bubble_cls = "bubble-quick" if is_quick else "bubble-bot"
            src_html   = _src_html(sources)

            st.markdown(
                f"<div class='{bubble_cls}'>"
                f"<span class='blabel'>Assistant{badge}</span>"
                f"{content}"
                f"{src_html}"
                f"</div>",
                unsafe_allow_html=True,
            )

            # Follow-up buttons (last message only)
            if idx == len(st.session_state["rag_messages"]) - 1 and sources:
                top_type = sources[0].get("metadata", {}).get("type", "")
                followups = FOLLOWUP_MAP.get(top_type, [])
                if followups:
                    st.markdown(
                        "<div style='margin:4px 80px 0 0;'>"
                        "<span style='font-size:0.6rem;color:#3a5a7a;font-weight:700;"
                        "text-transform:uppercase;letter-spacing:0.5px;'>Follow-up</span></div>",
                        unsafe_allow_html=True,
                    )
                    fu_cols = st.columns(len(followups))
                    for fi, fq in enumerate(followups):
                        with fu_cols[fi]:
                            if st.button(fq, key=f"fu_{idx}_{fi}", use_container_width=True):
                                st.session_state["rag_pending"] = fq
                                st.rerun()


def _check_ollama():
    if not _RAG_AVAILABLE:
        return False, "RAG not available"
    engine = get_engine()
    ok, msg = engine.ping_ollama()
    st.session_state["rag_ollama_ok"] = ok
    return ok, msg


def _auto_index():
    if not _RAG_AVAILABLE:
        return False
    engine = get_engine()
    if engine.is_indexed():
        st.session_state["rag_indexed"] = True
        return True
    with st.spinner("Building knowledge base — first-time setup…"):
        try:
            engine.build_index()
            st.session_state["rag_indexed"] = True
            return True
        except Exception as e:
            st.error(f"Index build failed: {e}")
            return False


def _run_rag(query: str):
    query = query.strip()
    if not query:
        return

    st.session_state["rag_messages"].append({"role": "user", "content": query})
    st.session_state["rag_pending"] = ""
    st.session_state["rag_fkey"]   += 1

    if not _auto_index():
        return

    engine  = get_engine()
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state["rag_messages"][:-1]
    ][-4:]

    with st.spinner("Searching knowledge base…"):
        docs  = engine.retrieve(query, k=5)
        quick = engine._quick_answer(query, docs)

    if quick:
        st.session_state["rag_messages"].append({
            "role":     "assistant",
            "content":  quick,
            "sources":  docs,
            "is_quick": True,
        })
        return

    with st.spinner("Generating answer…"):
        response, sources, err = engine.answer(query, chat_history=history, k=5)

    if err:
        if "timed out" in err.lower() or "timeout" in err.lower():
            content = (
                "**Model is loading into memory** — this happens once after Ollama restarts.\n\n"
                "Click **Warm Up Model** in the sidebar, wait ~15 seconds, then resend your question.\n\n"
                "> Your question: *" + query + "*"
            )
        elif "cannot reach" in err.lower() or "not running" in err.lower():
            content = (
                "**Ollama is not reachable.**\n\n"
                "Open a terminal and run:\n```\nollama serve\n```\n"
                "Then send your message again."
            )
        else:
            content = f"**Error:** {err}\n\nTry again or check that Ollama is running."
        sources  = docs
        is_quick = False
    else:
        content  = response or "Could not generate an answer — try rephrasing."
        is_quick = False

    st.session_state["rag_messages"].append({
        "role":     "assistant",
        "content":  content,
        "sources":  sources,
        "is_quick": is_quick,
    })


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown(
        "<div style='font-size:1rem;font-weight:800;color:#e8eef8;margin-bottom:2px;'>"
        "Career Chatbot</div>"
        "<div style='font-size:0.68rem;color:#7a9ec0;margin-bottom:14px;'>"
        "Retrieval-Augmented Generation</div>",
        unsafe_allow_html=True,
    )

    if not _RAG_AVAILABLE:
        st.error(f"RAG engine unavailable: {_rag_err_msg}")
    else:
        engine = get_engine()

        # Backend badge
        bc = "#22c55e" if _CHROMA_OK else "#f59e0b"
        bl = "ChromaDB + Embeddings" if _CHROMA_OK else "TF-IDF (sklearn)"
        st.markdown(
            f"<div class='sb-badge' style='background:{bc}10;border:1px solid {bc}30;color:{bc};'>"
            f"{bl}"
            f"</div>",
            unsafe_allow_html=True,
        )

        # Ollama status
        ollama_ok = st.session_state["rag_ollama_ok"]
        if ollama_ok is None:
            with st.spinner("Checking Ollama…"):
                ollama_ok, _ = _check_ollama()

        if ollama_ok:
            warmed = st.session_state["rag_warmed"]
            w_col  = "#22c55e" if warmed else "#f59e0b"
            w_lbl  = "Model warm — ready" if warmed else "Model cold — first message may be slow"
            st.markdown(
                f"<div style='font-size:0.7rem;color:{w_col};font-weight:700;margin-bottom:8px;'>"
                f"Ollama running &nbsp; · &nbsp; {w_lbl}</div>",
                unsafe_allow_html=True,
            )
            if not warmed:
                if st.button("Warm Up Model", use_container_width=True, key="warmup_btn"):
                    with st.spinner("Loading model into memory (~15 s)…"):
                        ok, err = engine.warmup_model()
                    if ok:
                        st.session_state["rag_warmed"] = True
                        st.success("Model ready.")
                        time.sleep(0.5)
                        st.rerun()
                    else:
                        st.warning("Still loading — try sending a message in ~10 s")
        else:
            st.markdown(
                "<div style='font-size:0.7rem;color:#ef4444;font-weight:700;margin-bottom:4px;'>"
                "Ollama not running</div>"
                "<div style='font-size:0.67rem;color:#7a9ec0;margin-bottom:8px;'>"
                "Run in terminal: <code style='color:#7ec8f7;'>ollama serve</code></div>",
                unsafe_allow_html=True,
            )
            if st.button("Re-check", use_container_width=True, key="recheck_btn"):
                st.session_state["rag_ollama_ok"] = None
                st.rerun()

        st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)

        # Index status
        if engine.is_indexed():
            st.markdown(
                f"<div style='color:#22c55e;font-size:0.7rem;font-weight:700;margin-bottom:8px;'>"
                f"{engine.doc_count} documents indexed</div>",
                unsafe_allow_html=True,
            )
        else:
            st.warning("Index not built — auto-builds on first message")
            if st.button("Build Index", use_container_width=True, key="build_idx"):
                prog = st.progress(0)
                def _cb(p, m):
                    prog.progress(min(p, 100), text=m)
                try:
                    n = engine.build_index(progress_cb=_cb)
                    prog.empty()
                    st.success(f"{n} documents indexed.")
                    time.sleep(0.5)
                    st.rerun()
                except Exception as ex:
                    prog.empty()
                    st.error(str(ex))

    st.divider()

    turns    = len(st.session_state["rag_messages"]) // 2
    quick_ct = sum(
        1 for m in st.session_state["rag_messages"]
        if m.get("is_quick") and m["role"] == "assistant"
    )
    st.markdown(
        f"<div style='font-size:0.68rem;color:#7a9ec0;margin-bottom:8px;'>"
        f"{turns} exchange{'s' if turns != 1 else ''}"
        + (f" · {quick_ct} instant" if quick_ct else "")
        + "</div>",
        unsafe_allow_html=True,
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Clear", use_container_width=True, key="sb_clear"):
            st.session_state["rag_messages"] = []
            st.session_state["rag_fkey"]    += 1
            st.session_state["rag_pending"]  = ""
            st.rerun()
    with c2:
        if st.button("Rebuild Index", use_container_width=True, key="sb_rebuild"):
            if _RAG_AVAILABLE:
                get_engine().reset_index()
                st.session_state["rag_indexed"] = False
                st.rerun()

        st.divider()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

# Header
hc1, hc2 = st.columns([3, 1])
with hc1:
    st.markdown(
        "<h1 style='margin:0 0 2px 0;font-size:1.6rem;font-weight:900;color:#e8eef8;'>"
        "Internship Career Chatbot</h1>"
        "<p style='font-size:0.8rem;color:#7a9ec0;margin:0;'>"
        "Ask about stipends, skills, companies, and roles. "
        "Answers grounded in real data. Simple queries answered instantly.</p>",
        unsafe_allow_html=True,
    )
with hc2:
    if _RAG_AVAILABLE:
        e = get_engine()
        st.markdown(
            f"<div style='text-align:right;padding-top:8px;'>"
            f"<span style='background:rgba(74,144,217,0.08);"
            f"border:1px solid rgba(74,144,217,0.25);border-radius:8px;"
            f"padding:5px 12px;font-size:0.68rem;color:#7ec8f7;font-weight:700;'>"
            f"{e.doc_count} documents</span></div>",
            unsafe_allow_html=True,
        )

if not _RAG_AVAILABLE:
    st.error(
        f"**`rag_engine.py` not found.**\n\n"
        f"Error: `{_rag_err_msg}`\n\n"
        "Place `rag_engine.py` alongside `intership_recommender.py`."
    )
    st.stop()

st.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)

chat_col, sug_col = st.columns([3, 1], gap="large")

# Suggestions column
with sug_col:
    for i, q in enumerate(SUGGESTIONS):
        if st.button(q, key=f"sug_{i}", use_container_width=True):
            _run_rag(q)
            st.rerun()

# Chat column
with chat_col:
    if not st.session_state["rag_messages"]:
        st.markdown(
            "<div style='text-align:center;padding:48px 24px;"
            "background:rgba(5,15,40,0.5);border:1px dashed rgba(74,144,217,0.18);"
            "border-radius:16px;margin-bottom:16px;'>"
            "<div style='font-size:1.1rem;font-weight:800;color:#e8eef8;"
            "margin-bottom:8px;'>Internship Advisor</div>"
            "<div style='font-size:0.82rem;color:#7a9ec0;max-width:420px;"
            "margin:0 auto;line-height:1.65;'>"
            "Covers <b style='color:#7ec8f7;'>34 Indian companies</b> — "
            "stipends, skills, ratings and locations. "
            "Answers are sourced directly from the knowledge base."
            "</div>"
            "<div style='margin-top:14px;display:flex;gap:10px;justify-content:center;"
            "flex-wrap:wrap;'>"
            "<span style='background:rgba(34,197,94,0.08);border:1px solid rgba(34,197,94,0.25);"
            "border-radius:20px;padding:3px 12px;font-size:0.68rem;color:#22c55e;"
            "font-weight:700;'>Instant</span>"
            "<span style='font-size:0.68rem;color:#7a9ec0;line-height:2;'>"
            "Company profiles and role skills answered without AI</span>"
            "<span style='background:rgba(74,144,217,0.08);border:1px solid rgba(74,144,217,0.25);"
            "border-radius:20px;padding:3px 12px;font-size:0.68rem;color:#7ec8f7;"
            "font-weight:700;'>AI</span>"
            "<span style='font-size:0.68rem;color:#7a9ec0;line-height:2;'>"
            "Comparisons and complex questions use Ollama</span>"
            "</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    else:
        _render_chat()
        st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)

    # Input form
    pending = st.session_state.get("rag_pending", "")
    with st.form(key=f"chat_form_{st.session_state['rag_fkey']}", clear_on_submit=True):
        fc1, fc2 = st.columns([6, 1])
        with fc1:
            user_text = st.text_input(
                "Question",
                value=pending,
                placeholder="Ask about stipends, skills, companies… (Enter to send)",
                label_visibility="collapsed",
            )
        with fc2:
            submitted = st.form_submit_button("Send", use_container_width=True, type="primary")

    if submitted and user_text.strip():
        _run_rag(user_text.strip())
        st.rerun()

    # Footer
    if st.session_state["rag_messages"]:
        e      = get_engine()
        turns  = len(st.session_state["rag_messages"]) // 2
        qc     = sum(1 for m in st.session_state["rag_messages"]
                     if m.get("is_quick") and m["role"] == "assistant")
        llm_ct = turns - qc
        st.markdown(
            f"<div style='font-size:0.6rem;color:#2a4a6a;margin-top:6px;text-align:center;'>"
            f"{e.backend_name} · {e.doc_count} docs · "
            f"{turns} turn(s) · {qc} instant · {llm_ct} LLM"
            f"</div>",
            unsafe_allow_html=True,
        )