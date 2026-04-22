import streamlit as st
import pandas as pd
from io import BytesIO
from datetime import date

st.set_page_config(page_title="Application Tracker", layout="wide")
st.markdown("# Application Tracker")

if "applications" not in st.session_state:
    st.session_state["applications"] = []

STATUSES = {
    "Saved": "#7a9ec0",
    "Applied": "#4a90d9",
    "Interview": "#f59e0b",
    "Offer": "#22c55e",
    "Rejected": "#ef4444"
}

with st.expander("+ Add Application", expanded=not st.session_state["applications"]):
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        co = st.text_input("Company", key="t_co")
    with c2:
        ro = st.text_input("Role", key="t_ro")
    with c3:
        dt = st.date_input("Date Applied", value=date.today(), key="t_dt")
    with c4:
        st_ = st.selectbox("Status", list(STATUSES), key="t_st")

    notes = st.text_input("Notes / Link", key="t_notes")

    if st.button("Add", type="primary"):
        if not co or not ro:
            st.error("Please enter both company and role.")
        else:
            st.session_state["applications"].append({
                "Company": co,
                "Role": ro,
                "Date": str(dt),
                "Status": st_,
                "Notes": notes
            })
            st.rerun()

apps = st.session_state["applications"]

if not apps:
    st.info("No applications yet.")
    st.stop()

df = pd.DataFrame(apps)

cols = st.columns(len(STATUSES))
for i, (s, c) in enumerate(STATUSES.items()):
    cnt = len(df[df["Status"] == s])
    cols[i].markdown(
        f"<div style='background:{c}18;border:1px solid {c}44;border-radius:8px;"
        f"padding:10px;text-align:center;'><b style='color:{c};font-size:1.5rem;'>{cnt}</b>"
        f"<div style='font-size:0.7rem;color:{c};'>{s}</div></div>",
        unsafe_allow_html=True
    )

st.markdown("---")

for i, row in enumerate(apps):
    c = STATUSES.get(row["Status"], "#7a9ec0")
    col1, col2, col3, col4 = st.columns([3, 2, 2, 1])

    with col1:
        st.markdown(f"**{row['Company']}** — {row['Role']}")
        if row.get("Notes"):
            st.caption(row["Notes"])

    with col2:
        st.markdown(
            f"<span style='color:{c};font-weight:700;'>{row['Status']}</span> · {row['Date']}",
            unsafe_allow_html=True
        )

    with col3:
        new_s = st.selectbox(
            "",
            list(STATUSES),
            index=list(STATUSES).index(row["Status"]),
            key=f"s_{i}",
            label_visibility="collapsed"
        )
        if new_s != row["Status"]:
            st.session_state["applications"][i]["Status"] = new_s
            st.rerun()

    with col4:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("✕", key=f"del_{i}"):
            st.session_state["applications"].pop(i)
            st.rerun()

buf = BytesIO()
df.to_csv(buf, index=False)
st.download_button("Download CSV", buf.getvalue(), "applications.csv", "text/csv")