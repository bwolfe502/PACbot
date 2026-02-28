#!/usr/bin/env python3
"""Generate PDF report for shoda's v1.4.2 bug report analysis."""

from fpdf import FPDF

pdf = FPDF()
pdf.set_auto_page_break(auto=True, margin=20)
pdf.add_page()

# Title
pdf.set_font("Helvetica", "B", 18)
pdf.cell(0, 12, "PACbot Bug Report Analysis", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Helvetica", "", 11)
pdf.set_text_color(100, 100, 100)
pdf.cell(0, 7, "Tester: shoda  |  Version: v1.4.2  |  Date: February 28, 2026", new_x="LMARGIN", new_y="NEXT")
pdf.set_text_color(0, 0, 0)
pdf.ln(4)

# Separator
pdf.set_draw_color(180, 180, 180)
pdf.line(10, pdf.get_y(), 200, pdf.get_y())
pdf.ln(6)

# Section: What You Reported
pdf.set_font("Helvetica", "B", 14)
pdf.cell(0, 9, "What You Reported", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Helvetica", "", 11)
pdf.ln(2)

reported = [
    "Gold mining doesn't wait long enough for the Gather button",
    "BlueStacks closed itself overnight",
    "Minimum troops for gold mines doesn't seem to work",
]
for item in reported:
    pdf.cell(6)
    pdf.cell(4, 7, "-")
    pdf.cell(0, 7, item, new_x="LMARGIN", new_y="NEXT")

pdf.ln(4)

# Section: What We Found in Your Data
pdf.set_font("Helvetica", "B", 14)
pdf.cell(0, 9, "What We Found in Your Data", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Helvetica", "", 11)
pdf.ln(2)

pdf.multi_cell(0, 6,
    "Your bug report data was excellent -34 hours of runtime, 4,277 actions, "
    "11 sessions, and 200 failure screenshots. We found several additional issues "
    "beyond what you reported:")
pdf.ln(3)

findings = [
    ("Gold Gathering (98% failure rate)",
     "669 attempts with only 13 successes. The bot was tapping coordinates "
     "instead of waiting for the Gather button to appear. This confirms your "
     "report -it was not waiting long enough."),
    ("Stuck Screen Loops (5-7 hours)",
     "What looked like BlueStacks crashing was actually the bot stuck in a loop. "
     "The MAP screen was scoring 73-78% confidence (just below the 80% threshold) "
     "so the bot did not recognize it. Most likely caused by Alliance Duel popups "
     "partially covering the screen."),
    ("Evil Guard Depart Button Misses",
     "The depart button confidence scores were 78-79% vs the 80% threshold -just "
     "barely missing. Emulator rendering causes slight variations."),
    ("Rally Titan Degradation",
     "Failure rate increased from 20% to 80% over 3 days. Likely caused by "
     "Alliance Duel event popups (the event ran all day Feb 28)."),
    ("Zombie Processes on Update",
     "When the bot updated itself, old processes were not cleaning up properly, "
     "leaving ADB connections hanging."),
]

for title, desc in findings:
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(6)
    pdf.cell(4, 7, "-")
    pdf.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    x = pdf.get_x()
    pdf.set_x(x + 10)
    pdf.multi_cell(170, 5.5, desc)
    pdf.ln(2)

pdf.ln(2)

# Section: Fixes Coming in v1.4.3
pdf.set_font("Helvetica", "B", 14)
pdf.cell(0, 9, "Fixes Coming in v1.4.3", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Helvetica", "", 11)
pdf.ln(2)

fixes = [
    "Rewrote gold gathering -now waits for the Gather button to actually appear "
    "before tapping, with retry logic",
    "Added smarter stuck screen recovery -4 escalating strategies to get unstuck "
    "instead of just retrying the same thing",
    "Lowered depart button threshold from 80% to 75% so it will not miss anymore",
    "Added version tracking to logs and stats (helps us diagnose issues faster)",
    "Sped up button detection in 5 areas based on your timing data",
    "Fixed the zombie process issue -bot now disconnects ADB cleanly on "
    "quit/restart",
    "Machine specs (CPU, RAM, OS) now auto-collected in bug reports",
]

for i, fix in enumerate(fixes, 1):
    pdf.cell(6)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(8, 7, f"{i}.")
    pdf.set_font("Helvetica", "", 10)
    w = 170
    pdf.multi_cell(w, 5.5, fix)
    pdf.ln(1)

pdf.ln(3)

# Section: What to Test Next
pdf.set_font("Helvetica", "B", 14)
pdf.cell(0, 9, "What to Test Next", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Helvetica", "", 11)
pdf.ln(2)

pdf.multi_cell(0, 6,
    "When v1.4.3 is ready, the main thing to check is gold gathering overnight. "
    "With the rewrite, the success rate should be dramatically better. "
    "Just run the bot with gather enabled like normal and send a new bug report "
    "zip afterward -we'll compare the numbers.")
pdf.ln(3)

pdf.multi_cell(0, 6,
    "If you see any Alliance Duel popups while the bot is running, a screenshot "
    "of what they look like would be really helpful so we can teach the bot to "
    "dismiss them automatically.")

pdf.ln(6)

# Footer
pdf.set_draw_color(180, 180, 180)
pdf.line(10, pdf.get_y(), 200, pdf.get_y())
pdf.ln(4)
pdf.set_font("Helvetica", "I", 9)
pdf.set_text_color(120, 120, 120)
pdf.cell(0, 6, "Thanks for testing! Your data directly drove all these fixes.", new_x="LMARGIN", new_y="NEXT")

# Save
output_path = "/Users/brian/Documents/PACbot/reports/shoda_v142_report.pdf"
pdf.output(output_path)
print(f"PDF saved to: {output_path}")
