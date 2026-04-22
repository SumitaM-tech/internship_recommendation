"""
ats_scorer.py — Improved ATS Resume Scorer
===========================================
Drop-in replacement.  No extra dependencies (stdlib + streamlit only).

Improvements over v1:
  • Job-description (JD) matching — pass jd_text to score_resume()
  • Word-boundary keyword matching — no more false positives
  • Multi-strategy skill counting (comma / semicolon / newline)
  • Smarter section-header detection (near line-start, not anywhere)
  • Richer soft-skill & action-verb banks
  • Curated missing-keywords list (top 10 most impactful, not whole bank)
  • JD Match category replaces static keyword category when JD is provided

score_resume(resume_text, parsed, jd_text) returns:
{
    "total":            72,
    "grade":            "B",
    "label":            "Good",
    "color":            "#4a90d9",
    "ring_pct":         72,
    "word_count":       420,
    "jd_match_pct":     68,          # None if no JD supplied
    "categories":       [...],
    "top_tips":         [...],
    "keywords_found":   [...],
    "keywords_missing": [...],
}
"""

import re
from typing import Any

# ── Keyword banks ─────────────────────────────────────────────────────────────

_ACTION_VERBS = {
    "developed","built","designed","implemented","engineered","created",
    "optimised","optimized","improved","reduced","increased","automated",
    "deployed","integrated","migrated","architected","led","managed",
    "collaborated","researched","analysed","analyzed","delivered","launched",
    "maintained","refactored","tested","debugged","documented","mentored",
    "contributed","resolved","coordinated","streamlined","established",
    "accelerated","authored","configured","customised","customized","enhanced",
    "executed","generated","modelled","modeled","monitored","overhauled",
    "prototyped","revamped","scaled","secured","visualized","wrote",
}

# Each entry: exact token (lowercase). Multi-word entries use spaces and are
# matched as phrase boundaries in the text.
_TECH_KEYWORDS: list[str] = [
    "python","java","javascript","typescript","c++","c#","golang","rust",
    "kotlin","swift","ruby","php","scala","r",
    "react","angular","vue","nextjs","nodejs","express","django","flask",
    "fastapi","spring","laravel","rails","svelte",
    "sql","mysql","postgresql","mongodb","redis","elasticsearch","firebase",
    "supabase","sqlite","cassandra","dynamodb","neo4j",
    "aws","gcp","azure","docker","kubernetes","terraform","ansible",
    "ci/cd","jenkins","github actions","gitlab ci","bitbucket",
    "git","linux","bash","rest","graphql","api","microservices","grpc",
    "machine learning","deep learning","nlp","computer vision","llm",
    "tensorflow","pytorch","scikit-learn","keras","pandas","numpy","opencv",
    "html","css","tailwind","bootstrap","sass","figma","webpack","vite",
    "android","ios","flutter","react native","swift ui","jetpack compose",
    "kafka","rabbitmq","celery","airflow","spark","hadoop","dbt",
    "langchain","openai","hugging face","rag","vector database",
]

_SOFT_KEYWORDS: list[str] = [
    "teamwork","communication","leadership","problem-solving","problem solving",
    "analytical","agile","scrum","cross-functional","stakeholder","presentation",
    "time management","adaptability","critical thinking","collaboration",
    "self-motivated","detail-oriented","fast learner","initiative","ownership",
]

_INTERN_TERMS = [
    "intern","internship","fresher","graduate","undergraduate","b.tech","btech",
    "b.e","mca","bca","b.sc","bsc","student","final year","third year",
]

_SECTION_MARKERS = {
    "summary":        ["summary","objective","profile","about me","career objective"],
    "skills":         ["skills","technologies","tech stack","tools","technical skills","core competencies"],
    "experience":     ["experience","internship","employment","work history","work experience"],
    "projects":       ["projects","project","personal projects","academic projects"],
    "education":      ["education","academic","qualification","academics"],
    "certifications": ["certification","certificate","achievements","courses","awards","licenses"],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _wb(kw: str) -> re.Pattern:
    """Compile a word-boundary regex for a keyword (handles C++, CI/CD, etc.)"""
    escaped = re.escape(kw)
    # Use \b only around alphanum edges; for special-char tokens wrap with
    # lookahead/lookbehind for whitespace or start/end.
    if re.search(r"[^a-z0-9]", kw):
        return re.compile(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", re.I)
    return re.compile(r"\b" + escaped + r"\b", re.I)

# Pre-compile patterns once at import time
_TECH_PATTERNS   = [(kw, _wb(kw)) for kw in _TECH_KEYWORDS]
_SOFT_PATTERNS   = [(kw, _wb(kw)) for kw in _SOFT_KEYWORDS]
_INTERN_PATTERNS = [(t,  _wb(t))  for t  in _INTERN_TERMS]

_NUM_PATTERN = re.compile(
    r"\d+[\s%xX×]|\d+\s*(?:percent|users|ms|seconds|requests|times|"
    r"hours|days|projects|modules|features|lines|records|api|endpoints|"
    r"commits|prs|tickets|bugs|repos|latency|throughput|accuracy|score)",
    re.I,
)

_DATE_PATTERN = re.compile(
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s,\-–]*20\d\d"
    r"|20\d\d\s*[\-–]\s*(?:20\d\d|present|current|now)",
    re.I,
)

def _section_present(lower_text: str, markers: list[str]) -> bool:
    """Check if a section header appears near a line start (not buried in body text)."""
    for marker in markers:
        # Match marker at start of a line (optionally preceded by spaces/bullets/emojis)
        pattern = re.compile(
            r"(?:^|[\n\r])[\s\•\-\*►▶→]*" + re.escape(marker) + r"\s*[:\-\n\r]",
            re.I | re.MULTILINE,
        )
        if pattern.search(lower_text):
            return True
    return False

def _count_skills(parsed: dict, text: str) -> int:
    """Count skills using multiple delimiters, falling back to tech keyword hits."""
    raw = parsed.get("skills") or ""
    if raw:
        # Try comma, semicolon, pipe, newline as delimiters
        for delim_pat in [r",", r";", r"\|", r"\n"]:
            parts = [s.strip() for s in re.split(delim_pat, raw) if s.strip()]
            if len(parts) >= 2:
                return len(parts)
    # Fallback: count tech keyword hits in full text
    return sum(1 for _, pat in _TECH_PATTERNS if pat.search(text))

def _jd_overlap(resume_lower: str, jd_lower: str) -> tuple[float, list[str], list[str]]:
    """
    Compute simple token-level overlap between resume and JD.
    Returns (pct_match, matched_terms, missing_terms).
    """
    # Extract meaningful tokens from JD (≥3 chars, no stopwords)
    _STOP = {
        "the","and","for","are","you","this","that","with","will","our",
        "have","from","your","about","more","what","they","into","their",
        "can","has","but","not","who","how","all","its","been","than",
        "were","also","both","each","must","need","should","such","use",
        "may","one","two","any","very","only","over","able","good","work",
    }
    jd_tokens = set(
        t.lower() for t in re.findall(r"[a-z][a-z0-9\+\#\./\-]{2,}", jd_lower)
        if t.lower() not in _STOP
    )
    if not jd_tokens:
        return 0.0, [], []
    matched  = [t for t in sorted(jd_tokens) if re.search(r"\b" + re.escape(t) + r"\b", resume_lower)]
    missing  = [t for t in sorted(jd_tokens) if t not in set(matched)]
    pct      = round(len(matched) / len(jd_tokens) * 100) if jd_tokens else 0
    # Surface only multi-char impactful terms in UI
    matched  = sorted(matched, key=len, reverse=True)[:15]
    missing  = sorted(missing, key=len, reverse=True)[:12]
    return pct, matched, missing


# ═════════════════════════════════════════════════════════════════════════════
# MAIN SCORER
# ═════════════════════════════════════════════════════════════════════════════

def score_resume(
    resume_text: str,
    parsed: dict | None = None,
    jd_text: str | None = None,
) -> dict:
    """
    Score resume text against ATS criteria.

    Args:
        resume_text: Raw resume string (plain text extracted from PDF).
        parsed:      Dict from your resume parser (optional but improves accuracy).
        jd_text:     Job description text (optional). When provided, replaces the
                     generic keyword category with a JD-match category.

    Returns a result dict — see module docstring.
    """
    parsed = parsed or {}
    text   = resume_text or ""
    lower  = text.lower()
    lines  = [l.strip() for l in text.splitlines() if l.strip()]

    categories = []

    # ── 1. CONTACT INFORMATION  (max 15) ─────────────────────────────────────
    c_score, c_tips = 0, []

    has_email = bool(re.search(r"[\w.+\-]+@[\w\-]+\.[a-z]{2,}", lower))
    has_phone = bool(re.search(r"\+?[\d][\d\s\-(). ]{7,14}\d", text))
    has_name  = bool(parsed.get("name") or (lines and 1 <= len(lines[0].split()) <= 6))
    has_li    = bool(re.search(r"linkedin\.com/in/[\w\-]+", lower))
    has_gh    = bool(re.search(r"github\.com/[\w\-]+", lower))
    has_loc   = bool(re.search(
        r"\b(bengaluru|bangalore|mumbai|delhi|chennai|hyderabad|pune|kolkata|"
        r"india|karnataka|maharashtra|telangana|[a-z]+,\s*india)\b", lower))

    if has_name:  c_score += 3
    else:         c_tips.append("Add your full name clearly at the top")
    if has_email: c_score += 3
    else:         c_tips.append("Add a professional email address")
    if has_phone: c_score += 2
    else:         c_tips.append("Add your phone number")
    if has_loc:   c_score += 2
    else:         c_tips.append("Add your city/state (e.g. Bengaluru, Karnataka)")
    if has_li:    c_score += 3
    else:         c_tips.append("Add your LinkedIn profile URL (linkedin.com/in/...)")
    if has_gh:    c_score += 2
    else:         c_tips.append("Add your GitHub profile URL (github.com/...)")

    categories.append({"name": "Contact Info", "score": c_score, "max": 15,
                        "pct": round(c_score / 15 * 100), "tips": c_tips, "icon": "📋"})

    # ── 2. SECTION COMPLETENESS  (max 20) ────────────────────────────────────
    s_score, s_tips = 0, []
    section_weights = {
        "summary": 3, "skills": 4, "experience": 5,
        "projects": 4, "education": 3, "certifications": 1,
    }
    for sec, weight in section_weights.items():
        found = _section_present(lower, _SECTION_MARKERS[sec])
        # Also accept parsed data as evidence
        if not found:
            pk = {"certifications": "certs"}.get(sec, sec)
            found = bool(parsed.get(pk))
        if found:
            s_score += weight
        else:
            s_tips.append(f"Add a '{sec.title()}' section")

    categories.append({"name": "Sections Present", "score": s_score, "max": 20,
                        "pct": round(s_score / 20 * 100), "tips": s_tips, "icon": "🗂️"})

    # ── 3. CONTENT QUALITY  (max 25) ─────────────────────────────────────────
    q_score, q_tips = 0, []

    # Summary length
    summary_text = parsed.get("summary") or ""
    sum_words = len(summary_text.split())
    if sum_words >= 40:   q_score += 5
    elif sum_words >= 20: q_score += 3
    elif sum_words >= 5:  q_score += 1
    else: q_tips.append("Write a professional summary (40+ words describing your profile)")

    # Skill count (multi-delimiter aware)
    skill_count = _count_skills(parsed, text)
    if skill_count >= 10:  q_score += 5
    elif skill_count >= 5: q_score += 3
    elif skill_count >= 2: q_score += 1
    else: q_tips.append("List at least 8–10 skills (tools, languages, frameworks)")

    # Experience bullets
    exp_list   = parsed.get("experience") or []
    total_buls = sum(len([b for b in e.get("bullets", []) if b]) for e in exp_list)
    if total_buls >= 6:   q_score += 5
    elif total_buls >= 3: q_score += 3
    elif total_buls >= 1: q_score += 1
    else: q_tips.append("Add 2–3 bullet points per experience/internship entry")

    # Quantified achievements
    all_bullet_text = " ".join(
        b for e in exp_list for b in e.get("bullets", [])
    ) + " ".join(
        b for p in (parsed.get("projects") or []) for b in p.get("bullets", [])
    )
    # If no parsed bullets, scan entire text for numbers near impact words
    if not all_bullet_text.strip():
        all_bullet_text = text

    num_hits = len(_NUM_PATTERN.findall(all_bullet_text))
    if num_hits >= 4:   q_score += 5
    elif num_hits >= 2: q_score += 3
    elif num_hits >= 1: q_score += 1
    else: q_tips.append("Quantify achievements (e.g. 'reduced load time by 40%', 'served 500+ users')")

    # Action verbs (check first word of each bullet + scan entire text)
    first_words: set[str] = set()
    for e in exp_list:
        for b in e.get("bullets", []):
            if b.split():
                first_words.add(b.split()[0].lower().rstrip(".,"))
    # Also scan lines that look like bullets in raw text
    for line in lines:
        clean = line.lstrip("•▸►-–—*◦▪ \t")
        if clean and len(clean.split()) >= 3:
            fw = clean.split()[0].lower().rstrip(".,")
            first_words.add(fw)

    verb_hits = len(first_words & _ACTION_VERBS)
    if verb_hits >= 5:   q_score += 5
    elif verb_hits >= 2: q_score += 3
    elif verb_hits >= 1: q_score += 1
    else: q_tips.append("Start bullet points with strong action verbs (Built, Developed, Designed…)")

    categories.append({"name": "Content Quality", "score": q_score, "max": 25,
                        "pct": round(q_score / 25 * 100), "tips": q_tips, "icon": "✍️"})

    # ── 4A. JD MATCH  (max 25, replaces generic keyword category) ────────────
    jd_match_pct = None
    if jd_text and jd_text.strip():
        jd_lower = jd_text.lower()
        jd_pct, jd_matched, jd_missing = _jd_overlap(lower, jd_lower)
        jd_match_pct = jd_pct

        jd_score = 0
        jd_tips  = []

        if jd_pct >= 80:   jd_score = 25
        elif jd_pct >= 65: jd_score = 20
        elif jd_pct >= 50: jd_score = 14
        elif jd_pct >= 35: jd_score = 8
        elif jd_pct >= 20: jd_score = 4
        else:
            jd_tips.append("Resume has very low overlap with the job description")

        if jd_pct < 65:
            top_missing = jd_missing[:5]
            if top_missing:
                jd_tips.append("Add JD keywords: " + ", ".join(top_missing))
        if jd_pct < 80:
            jd_tips.append(f"JD match is {jd_pct}% — tailor your resume to this role")

        found_tech   = [kw for kw, pat in _TECH_PATTERNS if pat.search(lower)]
        missing_tech = _prioritise_missing(lower, found_tech)

        categories.append({"name": f"JD Match ({jd_pct}%)", "score": jd_score, "max": 25,
                            "pct": jd_pct, "tips": jd_tips, "icon": "🎯"})

    # ── 4B. KEYWORD DENSITY  (max 25, used when no JD supplied) ──────────────
    else:
        k_score, k_tips = 0, []

        found_tech = [kw for kw, pat in _TECH_PATTERNS if pat.search(lower)]

        if len(found_tech) >= 12:   k_score += 15
        elif len(found_tech) >= 7:  k_score += 10
        elif len(found_tech) >= 3:  k_score += 5
        elif len(found_tech) >= 1:  k_score += 2
        else: k_tips.append("Add more tech keywords (languages, frameworks, tools)")

        found_soft = [kw for kw, pat in _SOFT_PATTERNS if pat.search(lower)]
        if len(found_soft) >= 4:   k_score += 5
        elif len(found_soft) >= 2: k_score += 3
        elif len(found_soft) >= 1: k_score += 1
        else: k_tips.append("Mention soft skills (Agile, Communication, Leadership…)")

        has_intern = any(pat.search(lower) for _, pat in _INTERN_PATTERNS)
        if has_intern:
            k_score += 5
        else:
            k_tips.append("Mention your student/fresher/intern status clearly")

        missing_tech = _prioritise_missing(lower, found_tech)

        categories.append({"name": "Keywords", "score": k_score, "max": 25,
                            "pct": round(k_score / 25 * 100), "tips": k_tips, "icon": "🔑"})

    # ── 5. ATS FORMATTING  (max 15) ──────────────────────────────────────────
    f_score, f_tips = 3, []   # +3: no table/image detection needed (plain text = safe)

    if _DATE_PATTERN.search(lower):
        f_score += 3
    else:
        f_tips.append("Add dates to experience entries (e.g. 'Jun 2024 – Aug 2024')")

    edu_list = parsed.get("education") or []
    if edu_list and any(e.get("degree") for e in edu_list):
        f_score += 3
    elif re.search(r"\b(b\.?tech|b\.?e|m\.?tech|bca|mca|b\.?sc|m\.?sc|bachelor|master)\b", lower):
        f_score += 2
    else:
        f_tips.append("Include your degree name and institution clearly")

    cert_list = parsed.get("certs") or []
    if cert_list or re.search(r"\b(coursera|udemy|nptel|google|aws|microsoft|hackerrank|leetcode|certificate)\b", lower):
        f_score += 3
    else:
        f_tips.append("Add certifications (Coursera, NPTEL, AWS, Google, etc.)")

    word_count = len(text.split())
    if 250 <= word_count <= 900:
        f_score += 3
    elif word_count >= 120:
        f_score += 1
        suffix = f"aim for 400–700 words"
        f_tips.append(
            f"Resume is {'long' if word_count > 900 else 'short'} ({word_count} words) — {suffix}"
        )
    else:
        f_tips.append(f"Resume text is very short ({word_count} words) — add more content")

    categories.append({"name": "ATS Formatting", "score": f_score, "max": 15,
                        "pct": round(f_score / 15 * 100), "tips": f_tips, "icon": "⚙️"})

    # ── Totals ────────────────────────────────────────────────────────────────
    total = min(100, sum(c["score"] for c in categories))

    if total >= 85:   grade, label, color = "A", "Excellent",  "#22c55e"
    elif total >= 70: grade, label, color = "B", "Good",        "#4a90d9"
    elif total >= 55: grade, label, color = "C", "Average",     "#f59e0b"
    elif total >= 40: grade, label, color = "D", "Needs Work",  "#f97316"
    else:             grade, label, color = "F", "Poor",        "#ef4444"

    # Top tips: pick from worst-scoring categories first
    all_tips: list[str] = []
    for c in sorted(categories, key=lambda x: x["pct"]):
        all_tips.extend(c["tips"])
    top_tips = all_tips[:5]

    return {
        "total":            total,
        "grade":            grade,
        "label":            label,
        "color":            color,
        "ring_pct":         total,
        "word_count":       word_count,
        "jd_match_pct":     jd_match_pct,
        "categories":       categories,
        "top_tips":         top_tips,
        "keywords_found":   found_tech[:15],
        "keywords_missing": missing_tech[:10],
    }


def _prioritise_missing(lower: str, found: list[str]) -> list[str]:
    """
    Return the most impactful missing keywords.
    Prefer multi-word / longer terms (more specific = more ATS value).
    """
    found_set = set(found)
    missing = [
        kw for kw, pat in _TECH_PATTERNS
        if kw not in found_set and not pat.search(lower)
    ]
    # Sort by length desc so multi-word keywords (more specific) appear first
    missing.sort(key=len, reverse=True)
    return missing[:10]


# ═════════════════════════════════════════════════════════════════════════════
# STREAMLIT RENDERER  (unchanged API — drop-in replacement)
# ═════════════════════════════════════════════════════════════════════════════

def render_ats_card(result: dict) -> None:
    """Renders an ATS score card via st.components.v1.html."""
    import streamlit as st
    import streamlit.components.v1 as components

    total = result["total"]
    color = result["color"]
    grade = result["grade"]
    label = result["label"]
    cats  = result["categories"]
    tips  = result["top_tips"]
    kw_f  = result["keywords_found"]
    kw_m  = result["keywords_missing"]
    wc    = result["word_count"]
    jd_pct = result.get("jd_match_pct")

    # SVG score ring (r=42 → circumference ≈ 263.9)
    circ     = 263.9
    dash_val = round(circ * total / 100, 1)
    gap_val  = round(circ - dash_val, 1)

    ring_svg = (
        f'<svg width="130" height="130" viewBox="0 0 100 100">'
        f'<circle cx="50" cy="50" r="42" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="10"/>'
        f'<circle cx="50" cy="50" r="42" fill="none" stroke="{color}" stroke-width="10"'
        f' stroke-dasharray="{dash_val} {gap_val}" stroke-dashoffset="66"'
        f' stroke-linecap="round" transform="rotate(-90 50 50)"/>'
        f'<text x="50" y="46" text-anchor="middle" font-size="22" font-weight="900"'
        f' fill="{color}" font-family="Inter,system-ui,sans-serif">{total}</text>'
        f'<text x="50" y="61" text-anchor="middle" font-size="9" fill="{color}"'
        f' font-family="Inter,system-ui,sans-serif" font-weight="700">/100</text>'
        f'</svg>'
    )

    # Category bars
    bars_html = ""
    for c in cats:
        bc = "#ef4444" if c["pct"] < 50 else "#f59e0b" if c["pct"] < 70 else "#22c55e"
        bars_html += (
            f'<div style="margin-bottom:11px;">'
            f'<div style="display:flex;justify-content:space-between;'
            f'align-items:center;margin-bottom:4px;">'
            f'<span style="font-size:0.74rem;color:#c8d8f0;font-weight:600;">'
            f'{c["icon"]} {c["name"]}</span>'
            f'<span style="font-size:0.71rem;color:{bc};font-weight:800;">'
            f'{c["score"]}/{c["max"]}</span></div>'
            f'<div style="background:rgba(255,255,255,0.08);border-radius:6px;'
            f'height:7px;overflow:hidden;">'
            f'<div style="width:{min(c["pct"],100)}%;height:100%;background:{bc};'
            f'border-radius:6px;"></div></div></div>'
        )

    def pill(text: str, fg: str) -> str:
        return (
            f'<span style="display:inline-block;background:{fg}18;'
            f'border:1px solid {fg}44;border-radius:20px;'
            f'padding:2px 9px;font-size:0.67rem;font-weight:700;'
            f'color:{fg};margin:2px 3px 2px 0;">{text}</span>'
        )

    found_pills   = "".join(pill(kw, "#22c55e") for kw in kw_f) \
                    or '<span style="color:#4a6a8a;font-size:0.74rem;">None detected</span>'
    missing_pills = "".join(pill(kw, "#f87171") for kw in kw_m) \
                    or '<span style="color:#22c55e;font-size:0.74rem;">Great coverage!</span>'

    tips_html = "".join(
        f'<div style="display:flex;gap:8px;align-items:flex-start;margin-bottom:7px;">'
        f'<span style="color:#f59e0b;font-size:0.78rem;flex-shrink:0;margin-top:1px;">&#9889;</span>'
        f'<span style="font-size:0.76rem;color:#c8d8f0;line-height:1.45;">{t}</span>'
        f'</div>'
        for t in tips
    ) or '<div style="color:#22c55e;font-size:0.77rem;">&#127881; Looking great!</div>'

    jd_badge = (
        f'&nbsp;&#183;&nbsp; <span style="color:#4a90d9;">JD Match: {jd_pct}%</span>'
        if jd_pct is not None else ""
    )

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: transparent;
    font-family: Inter, system-ui, -apple-system, sans-serif;
    padding: 4px 2px 8px 2px;
  }}
  .card {{
    background: rgba(5,15,40,0.92);
    border: 1px solid {color}55;
    border-radius: 16px;
    padding: 20px 22px 22px;
  }}
  .header-row {{ display:flex; align-items:center; gap:18px; margin-bottom:18px; }}
  .score-label {{
    font-size:0.62rem; color:#7a9ec0; font-weight:700;
    text-transform:uppercase; letter-spacing:0.9px; margin-bottom:4px;
  }}
  .score-title {{
    font-size:1.65rem; font-weight:900; color:{color};
    line-height:1; display:flex; align-items:center; gap:10px;
  }}
  .grade-badge {{
    font-size:0.92rem; background:{color}22; border:1px solid {color}55;
    border-radius:8px; padding:3px 11px; font-weight:800; color:{color};
  }}
  .score-meta {{ font-size:0.71rem; color:#7a9ec0; margin-top:5px; }}
  .body-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:22px; }}
  .section-label {{
    font-size:0.64rem; font-weight:700; text-transform:uppercase;
    letter-spacing:0.6px; margin-bottom:10px;
  }}
  .divider {{ height:1px; background:rgba(255,255,255,0.07); margin:14px 0 12px; }}
</style>
</head>
<body>
<div class="card">
  <div class="header-row">
    <div style="flex-shrink:0;">{ring_svg}</div>
    <div>
      <div class="score-label">ATS Compatibility Score</div>
      <div class="score-title">{label} <span class="grade-badge">Grade {grade}</span></div>
      <div class="score-meta">{wc} words &nbsp;&#183;&nbsp; {len(kw_f)} tech keywords{jd_badge}</div>
    </div>
  </div>
  <div class="body-grid">
    <div>
      <div class="section-label" style="color:#7ec8f7;">&#128202; Score Breakdown</div>
      {bars_html}
    </div>
    <div>
      <div class="section-label" style="color:#f59e0b;">&#9889; Quick Fixes</div>
      {tips_html}
      <div class="divider"></div>
      <div class="section-label" style="color:#22c55e;">&#10003; Keywords Found</div>
      <div style="margin-bottom:12px;">{found_pills}</div>
      <div class="section-label" style="color:#f87171;">&#9888; Consider Adding</div>
      <div>{missing_pills}</div>
    </div>
  </div>
</div>
</body>
</html>"""

    bar_height   = len(cats) * 55
    right_height = len(tips) * 45 + 160
    total_height = max(480, min(160 + max(bar_height, right_height) + 50, 760))
    components.html(html_doc, height=total_height, scrolling=False)