"""Read a statement PDF and print what would be imported. Saves nothing.

Prints only the period, each account's last 4 digits, row counts and balances, never row
details, so it is safe to run on a real statement.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from budget.statement_pdf import StatementParseError, parse_statement_pdf


class Command(BaseCommand):
    help = "Check that a statement PDF can be read (no database changes)."

    def add_arguments(self, parser):
        parser.add_argument("path")

    def handle(self, *args, **options):
        try:
            sections = parse_statement_pdf(Path(options["path"]).read_bytes())
        except OSError as exc:
            raise CommandError(f"Cannot open the file: {exc.strerror}") from exc
        except StatementParseError as exc:
            raise CommandError(f"Not readable: {exc}") from exc
        first = sections[0]
        self.stdout.write(f"Period {first.period_start} to {first.period_end}")
        for section in sections:
            money_in = sum(line.amount for line in section.lines if line.amount > 0)
            money_out = -sum(line.amount for line in section.lines if line.amount < 0)
            self.stdout.write(
                f"  {section.account_label}: {len(section.lines)} rows, "
                f"in ${money_in:,.2f}, out ${money_out:,.2f}, "
                f"balance ${section.opening_balance:,.2f} -> ${section.closing_balance:,.2f} "
                "(every row checked against the bank's running balance)"
            )
