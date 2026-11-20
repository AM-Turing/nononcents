from django.contrib import admin

from .models import (
    Account,
    ApplicationSetting,
    AuditEvent,
    BalanceAdjustment,
    BillOccurrence,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    Income,
    IncomeAllocation,
    PayPeriod,
    PayPeriodBudget,
    PaySchedule,
    RecurringBill,
    Refund,
    Transfer,
    UserPreference,
)

for model in [
    Household,
    HouseholdMembership,
    Account,
    Category,
    PaySchedule,
    PayPeriod,
    Income,
    IncomeAllocation,
    Expense,
    Refund,
    Transfer,
    BalanceAdjustment,
    RecurringBill,
    BillOccurrence,
    PayPeriodBudget,
    ApplicationSetting,
    UserPreference,
    AuditEvent,
]:
    admin.site.register(model)

admin.site.site_header = "NoNonCents administration"
admin.site.site_title = "NoNonCents admin"
admin.site.index_title = "Database administration"
