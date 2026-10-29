# LeetCode (leetcode.com)

"Solve" means LeetCode itself shows **Accepted** after you press Submit. A
solution written to a file, or tested in your own Python, is NOT solving it.

## 1. Which problem
- **"The next question" / "the easiest one I haven't solved"**: call
  leetcode_next (difficulty "Easy" for easiest). It needs the user's LeetCode
  username the first time. If you do not have it, ask the user for it and
  STOP - do not open problems to guess what is solved.
- **A named problem**: https://leetcode.com/problems/<slug>/ - the slug is the
  title in lower case with dashes, e.g. add-two-numbers.

## 2. Open and read it
- browser_open https://leetcode.com/problems/<slug>/description/ , then
  browser_read. Note the examples and constraints.

## 3. Language, then code
- The language is the dropdown above the editor (browser_find shows it as a
  popupbutton, e.g. "Java"). Keep the language it already shows unless the
  user asked for another - never switch it on your own.
- To change it: browser_select(control=<current label>, option=<language>).
  It confirms the switch.
- Write the solution IN THAT LANGUAGE, keeping the class name and method
  signature from the starter code, and enter it with browser_set_editor.
  Never type code - the editor auto-indents and auto-closes brackets.
- Before every Run or Submit, check the language dropdown matches the code you
  entered. Code in the wrong language counts as a failed submission on the
  user's account.

## 4. Run
- browser_click "Run" (keyboard: cmd+' ). Wait about 5 seconds, then
  browser_read and find the Test Result panel: "Accepted" for the sample
  cases, or "Wrong Answer" / "Runtime Error" / "Compile Error" with details.
  Fix the code and run again until the samples pass.

## 5. Submit
- browser_click "Submit" (keyboard: cmd+Enter). Wait about 8 seconds, then
  browser_read. Success shows "Accepted" with runtime and memory. Anything
  else: read which test failed, fix, submit again.

## 6. Report
- Problem number and title, language, and LeetCode's verdict exactly as shown.
- If anything blocks you - not logged in, a CAPTCHA or "verify you are human",
  a Premium-only problem - stop and say so. Never solve a CAPTCHA, and never
  claim a problem was solved without seeing "Accepted".

## Coach mode (when the request carries "[LeetCode coach mode is ON]")
The user wants to learn, so do not solve it for them:
- Never write a full solution, never enter code with browser_set_editor, and
  never press Run or Submit on their behalf (unless they explicitly ask).
- "Hint" / "I'm stuck": call leetcode_coach(action="hint"). It returns the
  problem and which hint level they are on. Give exactly ONE hint at that level:
  1 = a nudge (what to notice), 2 = the approach (pattern / data structure),
  3 = step-by-step pseudo-code with no real code. Then stop.
- "Review my code" / "what's wrong": call leetcode_coach(action="review"). It
  returns the problem and the code in their editor. Point out bugs, missed edge
  cases, and the time/space complexity, as questions or short pointers; show at
  most one or two corrected lines if they're truly stuck.
- Encourage briefly when they get it right.
