"""评测台的 xfail 判定 —— CI 门禁的正确性就系在这几行上。

为什么值得单独测: `.github/workflows/ci.yml` 拿 `run_eval --replay` 的退出码当门禁,
而 xfail 是唯一能让"失败"不算数的机制。它做糙了就是个假绿口子 —— 评测台之前
出过同类问题 (落库断言算了却没接进退出码, 失败时 CI 照样绿)。

这里钉住三条性质:
  1. 没标 xfail 的用例失败, 照样是 BAD (xfail 不能变成万能豁免)
  2. 标了 xfail 的用例**出异常**仍然是 BAD —— 回放时抛异常意味着 cassette 缺失
     或代码炸了, 那是真问题, 不能被"已知不过"盖过去
  3. 标了 xfail 却通过了要报 XPASS, 好让人把标记摘掉 —— 否则它会变成一块永久
     遮羞布, 将来真回退了也不会响
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval.run_eval import _case_ok, _flag  # noqa: E402


def row(exact=True, err="", after_ok=None, xfail=None):
    return {"exact": exact, "err": err, "after_ok": after_ok, "xfail": xfail}


class CaseOkTest(unittest.TestCase):
    def test_all_three_conditions_required(self):
        self.assertTrue(_case_ok(row()))
        self.assertFalse(_case_ok(row(exact=False)))
        self.assertFalse(_case_ok(row(err="LLMError: boom")))
        self.assertFalse(_case_ok(row(after_ok=False)))

    def test_after_ok_none_means_not_applicable(self):
        """没有 after 断言的 case, after_ok 是 None —— 不能当成失败。"""
        self.assertTrue(_case_ok(row(after_ok=None)))


class FlagTest(unittest.TestCase):
    def test_plain_case(self):
        self.assertEqual(_flag(row()), "OK ")
        self.assertEqual(_flag(row(exact=False)), "BAD")

    def test_xfail_absorbs_accuracy_failure(self):
        self.assertEqual(_flag(row(exact=False, xfail="已知不过")), "XFAIL")
        self.assertEqual(_flag(row(after_ok=False, xfail="已知不过")), "XFAIL")

    def test_xfail_does_not_absorb_errors(self):
        """最要紧的一条: 异常不能被 xfail 盖住。"""
        self.assertEqual(
            _flag(row(exact=False, err="LLMError: 404", xfail="已知不过")), "BAD")
        self.assertEqual(
            _flag(row(exact=True, err="cassette 缺失", xfail="已知不过")), "BAD")

    def test_xfail_that_passes_is_xpass(self):
        self.assertEqual(_flag(row(xfail="已知不过")), "XPASS")

    def test_xfail_is_not_a_blanket_exemption(self):
        """没标 xfail 的失败, 不受别处的 xfail 影响 —— 逐条判定。"""
        self.assertEqual(_flag(row(exact=False, xfail=None)), "BAD")
        self.assertEqual(_flag(row(exact=False, xfail="")), "BAD")


class ExitCodeRuleTest(unittest.TestCase):
    """run_eval.main() 的退出码规则: 只有 BAD 会让它非 0。"""

    def _exit_code(self, rows):
        return 0 if all(_flag(r) in ("OK ", "XFAIL", "XPASS") for r in rows) else 1

    def test_xfail_alone_does_not_fail_the_run(self):
        self.assertEqual(
            self._exit_code([row(), row(exact=False, xfail="已知不过")]), 0)

    def test_one_real_failure_fails_the_run(self):
        self.assertEqual(
            self._exit_code([row(), row(exact=False, xfail="已知不过"),
                             row(exact=False)]), 1)

    def test_xfail_with_error_fails_the_run(self):
        self.assertEqual(
            self._exit_code([row(), row(err="cassette 缺失", xfail="已知不过")]), 1)


if __name__ == "__main__":
    unittest.main()
