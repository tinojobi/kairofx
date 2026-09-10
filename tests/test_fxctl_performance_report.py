import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


class FxctlPerformanceCommandTests(unittest.TestCase):
    def load_fxctl(self):
        path = Path(__file__).resolve().parents[1] / "fxctl"
        return SourceFileLoader("fxctl_module", str(path)).load_module()

    def test_parser_registers_performance_report_command(self):
        fxctl = self.load_fxctl()
        parser = fxctl.build_parser()

        args = parser.parse_args(["--json", "performance-report", "--format", "summary"])

        self.assertEqual(args.command, "performance-report")
        self.assertEqual(args.format, "summary")
        self.assertIs(args.func, fxctl.cmd_performance_report)

    def test_parser_registers_closed_performance_command_with_broker_option(self):
        fxctl = self.load_fxctl()
        parser = fxctl.build_parser()

        args = parser.parse_args(["--json", "closed-performance", "--broker", "oanda", "--count", "50"])

        self.assertEqual(args.command, "closed-performance")
        self.assertEqual(args.broker, "oanda")
        self.assertEqual(args.count, 50)
        self.assertIs(args.func, fxctl.cmd_closed_performance)

    def test_closed_performance_unimplemented_broker_exits_cleanly(self):
        fxctl = self.load_fxctl()
        args = type("Args", (), {"broker": "mt5", "count": 10, "json": True})()

        with self.assertRaises(SystemExit) as ctx:
            fxctl.cmd_closed_performance(args)

        self.assertEqual(ctx.exception.code, 2)

    def test_parser_registers_daily_digest_command(self):
        fxctl = self.load_fxctl()
        parser = fxctl.build_parser()

        args = parser.parse_args(["daily-digest", "--broker", "oanda", "--recent-days", "14"])

        self.assertEqual(args.command, "daily-digest")
        self.assertEqual(args.broker, "oanda")
        self.assertEqual(args.recent_days, 14)
        self.assertIs(args.func, fxctl.cmd_daily_digest)
    def test_parser_registers_backtest_command(self):
        fxctl = self.load_fxctl()
        parser = fxctl.build_parser()

        args = parser.parse_args(["--json", "backtest", "--count", "200"])

        self.assertEqual(args.command, "backtest")
        self.assertEqual(args.count, 200)
        self.assertIs(args.func, fxctl.cmd_backtest)


if __name__ == "__main__":
    unittest.main()
