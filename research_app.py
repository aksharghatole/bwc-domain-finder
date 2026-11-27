"""BWC Domain Finder: local, resumable company-domain discovery."""
import io
import sqlite3
import pandas as pd
import streamlit as st
from scraper import connect, lookup, eligible_companies, write_result

st.set_page_config(page_title="BWC Domain Finder — Research", page_icon="🔎", layout="wide")
st.title("BWC Domain Finder — Automated Research")
st.caption("Free multi-engine discovery (DuckDuckGo, Bing, Google via unofficial ddgs; Bing RSS fallback) + homepage evidence. Results are suggestions, not verified ownership. Runs locally; no paid API key.")
cx = connect()

uploaded = st.file_uploader("Upload CSV or XLSX with company names", type=["csv", "xlsx"])
if uploaded is not None:
    try:
        table = pd.read_excel(uploaded) if uploaded.name.lower().endswith(".xlsx") else pd.read_csv(uploaded, encoding="utf-8-sig")
        if table.empty:
            st.warning("The uploaded file has no rows.")
        else:
            cols = list(table.columns)
            company_col = st.selectbox("Company name column", cols, index=next((i for i, c in enumerate(cols) if "company" in str(c).lower()), 0))
            country_col = st.selectbox("Country column (optional)", ["None"] + cols, index=0)
            if st.button("Import company list", type="primary"):
                records = []
                for _, row in table.iterrows():
                    name = str(row.get(company_col, "")).strip()
                    if name and name.lower() != "nan":
                        country = str(row.get(country_col, "")).strip() if country_col != "None" else ""
                        records.append((name, country if country.lower() != "nan" else ""))
                unique = {}
                for name, country in records:
                    unique.setdefault(name, (name, country))
                st.session_state["companies"] = list(unique.values())
                if len(unique) < len(set(records)):
                    st.warning("Same-name companies in different countries share one saved record in this version. The first country is used.")
                st.success(f"Loaded {len(st.session_state['companies'])} unique company names.")
    except Exception as e:
        st.error(f"Cannot read file: {e}")

# Store the imported list in the active browser session. Results persist in SQLite on disk.
companies = st.session_state.get("companies", [])
rows = pd.read_sql_query("SELECT * FROM results ORDER BY company", cx)
m1, m2, m3 = st.columns(3)
m1.metric("Companies in this import", len(companies))
m2.metric("Results saved", len(rows))
m3.metric("Domains suggested", int((rows["domain"].fillna("") != "").sum()) if not rows.empty else 0)

with st.expander("Search settings", expanded=True):
    batch = st.select_slider("Companies per run", options=[5, 10, 25, 50, 100, 250, 500, 1500], value=10)
    pause = st.slider("Delay between search requests (seconds)", 1.5, 8.0, 3.0, 0.5)
    retry_unmatched = st.checkbox("Retry researched companies without a domain", value=False)
    st.info("For unattended processing, select 1500 and keep this local app running. Search providers may rate-limit or block automated queries; a successful lookup is not guaranteed. Progress is saved after every company. Start with 5–10 to confirm searching works.")

if st.button("Find domains automatically", type="primary", disabled=not bool(companies)):
    pending = eligible_companies(companies, rows.fillna("").to_dict("records"), retry_unmatched)[:batch]
    if not pending:
        st.success("No eligible companies remain in this batch. Enable retry unmatched to research missing domains again.")
    else:
        progress = st.progress(0)
        status = st.empty()
        for i, (company, country) in enumerate(pending, 1):
            status.write(f"Researching {i}/{len(pending)}: **{company}**")
            try:
                result = lookup(company, country, pause=pause)
            except Exception as e:
                result = {"status": "review", "notes": f"Research error: {type(e).__name__}: {str(e)[:150]}"}
            write_result(cx, company, result)
            progress.progress(i / len(pending))
        st.success(f"Finished {len(pending)} companies. Saved to local_data/domain_finder.sqlite3")
        st.rerun()

st.subheader("Saved research")
rows = pd.read_sql_query("SELECT * FROM results ORDER BY company", cx)
if not rows.empty:
    view = rows.copy()
    view["status"] = view["status"].fillna("review")
    edited = st.data_editor(
        view, hide_index=True, use_container_width=True, num_rows="fixed",
        disabled=["company", "evidence_url", "evidence_title", "matched_name", "checked_at"],
        column_config={
            "evidence_url": st.column_config.LinkColumn("Evidence URL"),
            "confidence": st.column_config.NumberColumn("Confidence", min_value=0, max_value=100),
            "status": st.column_config.SelectboxColumn("Status", options=["review", "approved", "rejected"]),
        }, key="review_grid"
    )
    if st.button("Save reviewed domain/status changes"):
        for _, row in edited.iterrows():
            cx.execute("UPDATE results SET domain=?, confidence=?, status=?, notes=? WHERE company=?",
                       (str(row["domain"] or ""), int(row["confidence"] or 0),
                        str(row["status"] or "review"), str(row["notes"] or ""), str(row["company"])))
        cx.commit()
        st.rerun()
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        rows.to_excel(writer, index=False, sheet_name="Domains")
    st.download_button("Download Excel", output.getvalue(), file_name="bwc_company_domains.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.download_button("Download CSV", rows.to_csv(index=False).encode("utf-8-sig"),
                       file_name="bwc_company_domains.csv", mime="text/csv")
else:
    st.info("Import a CSV/XLSX and run your first batch to see domain candidates here.")

st.caption("Avoid bulk scraping websites where access is prohibited. DuckDuckGo search is accessed via an unofficial library; availability and permitted use may change. The app does not bypass CAPTCHAs or rate limits.")
