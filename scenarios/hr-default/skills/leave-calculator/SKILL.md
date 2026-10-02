---
name: leave-calculator
description: Calculate leave balances, plan optimal leave schedules, and check eligibility for various leave types
---
# Leave Calculator

Help employees plan their leave by:

1. Check current leave balance via hr-tools (check_leave_balance)
2. Calculate remaining days by leave type
3. Suggest optimal scheduling (avoid peak periods, consider team coverage)
4. Verify eligibility for special leave types (marriage, parental, bereavement)

## Output Format

| Leave Type | Entitled | Used | Remaining |
|-----------|----------|------|-----------|
| Annual    | 15 days  | 5    | 10        |
| Sick      | 10 days  | 2    | 8         |
| ...       | ...      | ...  | ...       |

Include recommendations for optimal scheduling.
