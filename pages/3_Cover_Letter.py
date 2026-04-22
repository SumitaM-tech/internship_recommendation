import sys, os, json, re
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import streamlit as st
import requests
from intership_recommender import read_resume

st.set_page_config(page_title="Cover Letter", layout="wide")

def _ollama(prompt, model="phi3:mini"):
    try:
        r = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.6,
                    "num_predict": 300   # 🔥 reduce tokens → faster
                }
            },
            timeout=180  # 🔥 increase timeout
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        return f"[Ollama error: {e}]"

st.markdown("# Cover Letter Generator")
left, right = st.columns(2)

with left:
    f = st.file_uploader("Resume (PDF/DOCX/TXT)", type=["pdf","docx","txt"])
    company = st.text_input("Company", placeholder="Google India")
    role    = st.text_input("Role", placeholder="Software Engineering Intern")
    jd      = st.text_area("Job Description (paste key skills or full JD)", height=160)
    tone    = st.selectbox("Tone", ["Professional", "Enthusiastic", "Concise"])
    model   = st.text_input("Ollama model", value="phi3:mini")

    if st.button("Generate Cover Letter", type="primary"):
        resume_text = read_resume(f) if f else st.session_state.get("resume_raw", "")
        if not resume_text or not role:
            st.error("Provide resume and role.")
        else:
            prompt = f"""Write a 3-paragraph internship cover letter.
Tone: {tone}
Candidate resume summary: {resume_text[:1500]}
Target: {role} at {company or 'the company'}
JD keywords: {jd[:600]}

Rules:
- Para 1: Opening hook + why this specific company
- Para 2: 2 achievements from resume matching the JD
- Para 3: Short call to action
- No placeholders like [Your Name]
- Return only the letter text, no subject line, no extra commentary."""

            with st.spinner("Writing via Ollama..."):
                result = _ollama(prompt, model)
            st.session_state["cover_letter"] = result
            st.session_state["cl_company"] = company
            st.session_state["cl_role"] = role

with right:
    if "cover_letter" in st.session_state:
        result = st.session_state["cover_letter"]
        st.text_area("Cover Letter", value=result, height=500)
        fname = f"cover_{(st.session_state.get('cl_company','co') or 'co').lower().replace(' ','_')}.txt"
        st.download_button("Download TXT", result.encode(), fname, "text/plain", use_container_width=True)
    else:
        st.markdown(
            "<div style='border:2px dashed rgba(74,144,217,0.2);border-radius:12px;"
            "padding:60px 24px;text-align:center;color:#3a5575;'>Cover letter appears here</div>",
            unsafe_allow_html=True
        )