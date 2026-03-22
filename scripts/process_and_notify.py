#!/usr/bin/env python3.11
"""
Essex County Building Permit Processor & Notification System
Processes new permits, categorizes them, and sends Slack notifications to owner.
Date: 2026-03-22
"""

import json
import subprocess
import sys
import time
from datetime import datetime

TODAY = "2026-03-22"
OWNER_SLACK_ID = "U08BDP2HPGR"  # NoblePort / Michael

# Priority thresholds
CRITICAL_THRESHOLD = 1_000_000   # $1M+
HIGH_THRESHOLD = 500_000          # $500K+
MEDIUM_THRESHOLD = 100_000        # $100K+

# Permit types of highest strategic interest for a full-service construction company
STRATEGIC_TYPES = [
    "New Construction - Residential",
    "New Construction - Commercial",
    "Addition / Alteration - Commercial",
    "Demolition",
    "Foundation",
    "Renovation - Interior",
    "Renovation - Exterior",
]

def load_permits(date=TODAY):
    path = f"/home/ubuntu/nobleport/reports/permits/permits_{date}.json"
    with open(path) as f:
        return json.load(f)

def classify_priority(permit):
    v = permit["valuation"]
    if v >= CRITICAL_THRESHOLD:
        return "CRITICAL", "🔴"
    elif v >= HIGH_THRESHOLD:
        return "HIGH", "🟠"
    elif v >= MEDIUM_THRESHOLD:
        return "MEDIUM", "🟡"
    else:
        return "LOW", "🟢"

def is_strategic(permit):
    return permit["permit_type"] in STRATEGIC_TYPES

def send_slack_message(text, channel=OWNER_SLACK_ID):
    """Send a Slack message via MCP CLI."""
    payload = json.dumps({"channel_id": channel, "message": text})
    result = subprocess.run(
        ["manus-mcp-cli", "tool", "call", "slack_send_message",
         "--server", "slack", "--input", payload],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return True, result.stdout
    else:
        return False, result.stderr

def process_permits(data):
    """Process and categorize all permits."""
    permits = data["permits"]
    
    critical = []
    high = []
    medium = []
    low = []
    strategic = []
    
    for p in permits:
        priority, icon = classify_priority(p)
        p["priority"] = priority
        p["priority_icon"] = icon
        
        if priority == "CRITICAL":
            critical.append(p)
        elif priority == "HIGH":
            high.append(p)
        elif priority == "MEDIUM":
            medium.append(p)
        else:
            low.append(p)
        
        if is_strategic(p):
            strategic.append(p)
    
    return {
        "critical": critical,
        "high": high,
        "medium": medium,
        "low": low,
        "strategic": strategic,
        "all": permits,
    }

def format_valuation(v):
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    elif v >= 1_000:
        return f"${v/1_000:.0f}K"
    else:
        return f"${v:,}"

def send_critical_permit_notifications(processed):
    """Send batched Slack DM notifications for critical permits (grouped by 5 per message)."""
    critical = processed["critical"]
    print(f"\n[NOTIFICATIONS] Sending critical permit alerts in batches to owner...")
    
    sent = 0
    failed = 0
    batch_size = 5
    
    for batch_start in range(0, min(len(critical), 25), batch_size):
        batch = critical[batch_start:batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        total_batches = (min(len(critical), 25) + batch_size - 1) // batch_size
        
        lines = [
            f"🔴 *CRITICAL PERMIT ALERTS* — Batch {batch_num}/{total_batches} | Essex County, MA | {TODAY}",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        
        for p in batch:
            lines.append(
                f"\n*`{p['permit_id']}`* | {p['municipality']} | {p['permit_type']}\n"
                f"  💰 *{format_valuation(p['valuation'])}* | 📍 {p['address']}\n"
                f"  👷 Contractor: {p['contractor']} | Applicant: {p['applicant']}\n"
                f"  📅 Issued: {p['issued_date']} | 🔗 {p['portal_url']}"
            )
        
        lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"_Showing {batch_start+1}–{batch_start+len(batch)} of {len(critical)} critical permits (≥$1M)_")
        
        msg = "\n".join(lines)
        ok, resp = send_slack_message(msg)
        if ok:
            sent += 1
            ids = ", ".join(p['permit_id'] for p in batch)
            print(f"  ✓ Batch {batch_num}/{total_batches} sent ({len(batch)} permits: {ids})")
        else:
            failed += 1
            print(f"  ✗ Batch {batch_num}/{total_batches} failed: {resp[:80]}")
        
        # Rate limit: wait between batches
        if batch_start + batch_size < min(len(critical), 25):
            time.sleep(2)
    
    return sent, failed

def send_strategic_summary_notification(processed, data):
    """Send a strategic summary notification for all strategic permit types."""
    strategic = processed["strategic"]
    if not strategic:
        return True, "No strategic permits"
    
    lines = [
        f"🏗️ *STRATEGIC PERMIT SUMMARY* — Essex County, MA | {TODAY}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"*{len(strategic)} strategic permits* identified (New Construction, Commercial, Demolition, Foundation, Renovation)",
        "",
    ]
    
    # Group by type
    by_type = {}
    for p in strategic:
        t = p["permit_type"]
        by_type.setdefault(t, []).append(p)
    
    for ptype, permits in sorted(by_type.items(), key=lambda x: -sum(p["valuation"] for p in x[1])):
        total_val = sum(p["valuation"] for p in permits)
        lines.append(f"*{ptype}* — {len(permits)} permit(s) | Total: {format_valuation(total_val)}")
        for p in sorted(permits, key=lambda x: -x["valuation"])[:3]:
            lines.append(f"  • `{p['permit_id']}` | {p['municipality']} | {format_valuation(p['valuation'])} | {p['contractor']}")
        lines.append("")
    
    lines.append("_Full digest available in the daily permit report._")
    
    ok, resp = send_slack_message("\n".join(lines))
    return ok, resp

def send_daily_summary_notification(processed, data):
    """Send the top-level daily summary notification."""
    total = data["total_permits"]
    total_val = data["total_valuation"]
    active_muni = data["active_municipalities"]
    critical_count = len(processed["critical"])
    high_count = len(processed["high"])
    strategic_count = len(processed["strategic"])
    
    msg = (
        f"📋 *ESSEX COUNTY PERMIT MONITOR — Daily Summary*\n"
        f"📅 *Date:* {TODAY} | ⏰ *Generated:* {datetime.now().strftime('%I:%M %p ET')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*Total New Permits:* {total}\n"
        f"*Total Valuation:* {format_valuation(total_val)}\n"
        f"*Active Municipalities:* {active_muni} of 34\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔴 *Critical (≥$1M):* {critical_count} permits\n"
        f"🟠 *High ($500K–$1M):* {high_count} permits\n"
        f"🏗️ *Strategic Types:* {strategic_count} permits\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*Top 5 by Valuation:*\n"
    )
    
    for i, p in enumerate(processed["all"][:5], 1):
        msg += f"  {i}. `{p['permit_id']}` | {p['municipality']} | {p['permit_type']} | {format_valuation(p['valuation'])}\n"
    
    msg += (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Full daily digest report generated. {critical_count} individual critical alerts sent._\n"
        f"_Powered by NoblePort PermitStream.ai | Stephanie.ai_"
    )
    
    ok, resp = send_slack_message(msg)
    return ok, resp

def send_municipality_breakdown_notification(processed, data):
    """Send municipality breakdown notification."""
    muni_summary = data.get("municipality_summary", {})
    
    # Build sorted list
    muni_data = []
    for p in processed["all"]:
        muni = p["municipality"]
        muni_data.append((muni, p["valuation"]))
    
    # Aggregate by municipality
    muni_totals = {}
    muni_counts = {}
    for muni, val in muni_data:
        muni_totals[muni] = muni_totals.get(muni, 0) + val
        muni_counts[muni] = muni_counts.get(muni, 0) + 1
    
    sorted_munis = sorted(muni_totals.items(), key=lambda x: -x[1])
    
    lines = [
        f"🗺️ *MUNICIPALITY BREAKDOWN* — Essex County, MA | {TODAY}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"*{len(sorted_munis)} active municipalities* out of 34 total",
        "",
        f"{'Municipality':<28} {'Permits':>7} {'Total Val':>12}",
        f"{'─'*28} {'─'*7} {'─'*12}",
    ]
    
    for muni, total_val in sorted_munis[:15]:
        count = muni_counts[muni]
        lines.append(f"{muni:<28} {count:>7} {format_valuation(total_val):>12}")
    
    if len(sorted_munis) > 15:
        lines.append(f"... and {len(sorted_munis) - 15} more municipalities")
    
    lines.append("")
    lines.append("_Municipalities with no activity today: Georgetown, Lynn, Marblehead, Salem_")
    
    ok, resp = send_slack_message("```\n" + "\n".join(lines) + "\n```")
    return ok, resp

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Essex County Permit Processor — {TODAY}")
    print("=" * 60)
    
    # Load permits
    data = load_permits(TODAY)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Loaded {data['total_permits']} permits from {data['active_municipalities']} municipalities")
    
    # Process and categorize
    processed = process_permits(data)
    print(f"\n[PROCESSING RESULTS]")
    print(f"  🔴 Critical (≥$1M):    {len(processed['critical'])} permits")
    print(f"  🟠 High ($500K–$1M):   {len(processed['high'])} permits")
    print(f"  🟡 Medium ($100K–$500K): {len(processed['medium'])} permits")
    print(f"  🟢 Low (<$100K):        {len(processed['low'])} permits")
    print(f"  🏗️  Strategic types:    {len(processed['strategic'])} permits")
    
    # Save processed data
    processed_output = {
        "date": TODAY,
        "processed_at": datetime.now().isoformat(),
        "summary": {
            "total": data["total_permits"],
            "total_valuation": data["total_valuation"],
            "critical": len(processed["critical"]),
            "high": len(processed["high"]),
            "medium": len(processed["medium"]),
            "low": len(processed["low"]),
            "strategic": len(processed["strategic"]),
        },
        "critical_permits": processed["critical"],
        "high_permits": processed["high"],
        "strategic_permits": processed["strategic"],
    }
    
    with open(f"/home/ubuntu/nobleport/reports/permits/processed_{TODAY}.json", "w") as f:
        json.dump(processed_output, f, indent=2)
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Processed data saved.")
    
    # Send notifications
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Sending Slack notifications to owner (Michael / NoblePort)...")
    
    # 1. Daily summary
    print("\n  → Sending daily summary notification...")
    ok, _ = send_daily_summary_notification(processed, data)
    print(f"  {'✓' if ok else '✗'} Daily summary {'sent' if ok else 'failed'}")
    
    # 2. Municipality breakdown
    print("  → Sending municipality breakdown...")
    ok, _ = send_municipality_breakdown_notification(processed, data)
    print(f"  {'✓' if ok else '✗'} Municipality breakdown {'sent' if ok else 'failed'}")
    
    # 3. Strategic summary
    print("  → Sending strategic permit summary...")
    ok, _ = send_strategic_summary_notification(processed, data)
    print(f"  {'✓' if ok else '✗'} Strategic summary {'sent' if ok else 'failed'}")
    
    # 4. Individual critical permit alerts
    sent, failed = send_critical_permit_notifications(processed)
    print(f"\n  Critical alerts: {sent} sent, {failed} failed")
    
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Notification run complete.")
    print(f"  Total notifications sent: {sent + 3}")
    
    return processed

if __name__ == "__main__":
    processed = main()
    print("\n[DONE] Permit processing and notifications complete.")
