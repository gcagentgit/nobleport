#!/usr/bin/env python3.11
"""
Essex County Building Permit Digest Generator
Generates a comprehensive Markdown and PDF report from processed permit data.
Date: 2026-03-22
"""

import json
import subprocess
from datetime import datetime

TODAY = "2026-03-22"

def load_processed_data():
    with open(f"/home/ubuntu/nobleport/reports/permits/processed_{TODAY}.json") as f:
        return json.load(f)

def load_raw_data():
    with open(f"/home/ubuntu/nobleport/reports/permits/permits_{TODAY}.json") as f:
        return json.load(f)

def format_valuation(v):
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    elif v >= 1_000:
        return f"${v/1_000:.0f}K"
    else:
        return f"${v:,}"

def generate_markdown(processed, raw):
    md = []
    
    # Header
    md.append(f"# Essex County Building Permit Daily Digest")
    md.append(f"## Sunday, March 22, 2026\n")
    md.append(f"> **Generated:** March 22, 2026 at {datetime.now().strftime('%I:%M %p')}  ")
    md.append(f"> **Coverage:** All 34 Essex County, Massachusetts Municipalities  ")
    md.append(f"> **Source:** Municipal Building Permit Portals (OpenGov, Accela, Generic)  ")
    md.append(f"> **Report Type:** Daily New Permit Monitor\n")
    md.append(f"---\n")
    
    # Executive Summary
    md.append(f"## Executive Summary\n")
    md.append(f"| Metric | Value |")
    md.append(f"|---|---|")
    md.append(f"| Total New Permits | **{raw['total_permits']}** |")
    md.append(f"| Total Construction Valuation | **{format_valuation(raw['total_valuation'])}** |")
    md.append(f"| Critical Permits (≥$1M) | **{processed['summary']['critical']}** |")
    md.append(f"| High-Value Permits ($500K–$1M) | **{processed['summary']['high']}** |")
    md.append(f"| Strategic Permits | **{processed['summary']['strategic']}** |")
    md.append(f"| Active Municipalities | **{raw['active_municipalities']} of 34** |\n")
    md.append(f"---\n")
    
    # Strategic Opportunities
    md.append(f"## Strategic Opportunities\n")
    md.append(f"These permits represent the highest strategic value for NoblePort's full-service construction and real estate development operations.\n")
    
    md.append(f"| Priority | Permit ID | Municipality | Type | Valuation | Address | Contractor |")
    md.append(f"|---|---|---|---|---|---|---|")
    
    # Add top 30 critical/strategic permits
    count = 0
    for p in processed["critical_permits"]:
        if count >= 30: break
        md.append(f"| 🔴 CRITICAL | `{p['permit_id']}` | {p['municipality']} | {p['permit_type']} | **{format_valuation(p['valuation'])}** | {p['address']} | {p['contractor']} |")
        count += 1
        
    for p in processed["high_permits"]:
        if count >= 40: break
        if p["permit_type"] in ["New Construction - Residential", "New Construction - Commercial", "Addition / Alteration - Commercial"]:
            md.append(f"| 🟠 HIGH | `{p['permit_id']}` | {p['municipality']} | {p['permit_type']} | **{format_valuation(p['valuation'])}** | {p['address']} | {p['contractor']} |")
            count += 1
            
    md.append(f"\n---\n")
    
    # Activity by Municipality
    md.append(f"## Activity by Municipality\n")
    md.append(f"| Municipality | Permits | Total Valuation | Avg Valuation |")
    md.append(f"|---|---|---|---|")
    
    muni_stats = {}
    for p in raw["permits"]:
        m = p["municipality"]
        if m not in muni_stats:
            muni_stats[m] = {"count": 0, "val": 0}
        muni_stats[m]["count"] += 1
        muni_stats[m]["val"] += p["valuation"]
        
    for m, stats in sorted(muni_stats.items(), key=lambda x: -x[1]["val"]):
        avg = stats["val"] / stats["count"]
        md.append(f"| {m} | {stats['count']} | {format_valuation(stats['val'])} | {format_valuation(avg)} |")
        
    md.append(f"\n---\n")
    
    # Permits by Type
    md.append(f"## Permits by Type\n")
    md.append(f"| Permit Type | Count | Total Valuation | % of Total |")
    md.append(f"|---|---|---|---|")
    
    type_stats = {}
    for p in raw["permits"]:
        t = p["permit_type"]
        if t not in type_stats:
            type_stats[t] = {"count": 0, "val": 0}
        type_stats[t]["count"] += 1
        type_stats[t]["val"] += p["valuation"]
        
    for t, stats in sorted(type_stats.items(), key=lambda x: -x[1]["val"]):
        pct = (stats["val"] / raw["total_valuation"]) * 100
        md.append(f"| {t} | {stats['count']} | {format_valuation(stats['val'])} | {pct:.1f}% |")
        
    md.append(f"\n---\n")
    
    # Complete Register
    md.append(f"## Complete Permit Register\n")
    md.append(f"All new permits issued today across Essex County, sorted by valuation (descending).\n")
    
    md.append(f"| # | Permit ID | Municipality | Type | Valuation | Address | Applicant | Contractor | Issued |")
    md.append(f"|---|---|---|---|---|---|---|---|---|")
    
    for i, p in enumerate(raw["permits"], 1):
        md.append(f"| {i} | `{p['permit_id']}` | {p['municipality']} | {p['permit_type']} | {format_valuation(p['valuation'])} | {p['address']} | {p['applicant']} | {p['contractor']} | {p['issued_date']} |")
        
    return "\n".join(md)

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Generating Daily Digest for {TODAY}...")
    
    processed = load_processed_data()
    raw = load_raw_data()
    
    md_content = generate_markdown(processed, raw)
    
    md_path = f"/home/ubuntu/nobleport/reports/permits/permit_digest_{TODAY}.md"
    pdf_path = f"/home/ubuntu/nobleport/reports/permits/permit_digest_{TODAY}.pdf"
    
    with open(md_path, "w") as f:
        f.write(md_content)
    print(f"  ✓ Markdown saved: {md_path}")
    
    # Convert to PDF
    print(f"  → Converting to PDF...")
    result = subprocess.run(
        ["manus-md-to-pdf", md_path, pdf_path],
        capture_output=True, text=True
    )
    
    if result.returncode == 0:
        print(f"  ✓ PDF saved: {pdf_path}")
    else:
        print(f"  ✗ PDF conversion failed: {result.stderr}")

if __name__ == "__main__":
    main()
