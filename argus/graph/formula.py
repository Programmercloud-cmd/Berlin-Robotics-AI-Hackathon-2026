"""Safe arithmetic formulas over metric ids, plus a tiny unit algebra.

A formula like ``sessions * add_to_cart_rate * checkout_start_rate`` is parsed
with ``ast`` (only + - * / and numeric constants are allowed), so it can be:
  * evaluated on numbers, numpy arrays or pandas Series,
  * inspected for each variable's *role* (factor / divisor / addend / subtrahend)
    and *polarity* (does an increase of the input raise or lower the output?),
  * type-checked for unit consistency (``count * EUR/count -> EUR``).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from fractions import Fraction

_BIN = (ast.Add, ast.Sub, ast.Mult, ast.Div)


class FormulaError(ValueError):
    pass


@dataclass(frozen=True)
class VarRole:
    role: str      # factor | divisor | addend | subtrahend | term
    polarity: int  # +1: input up => output up ; -1: input up => output down (assuming positive values)


@dataclass
class Formula:
    expr: str
    tree: ast.Expression = field(repr=False)

    @classmethod
    def parse(cls, expr: str) -> "Formula":
        try:
            tree = ast.parse(expr.strip(), mode="eval")
        except SyntaxError as e:
            raise FormulaError(f"cannot parse formula {expr!r}: {e.msg}") from None
        for node in ast.walk(tree):
            ok = isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load, ast.USub, ast.UAdd)
                            + _BIN) or (isinstance(node, ast.Constant) and isinstance(node.value, (int, float)))
            if not ok:
                raise FormulaError(f"unsupported syntax in formula {expr!r}: {type(node).__name__}")
        return cls(expr.strip(), tree)

    @property
    def variables(self) -> list[str]:
        seen: list[str] = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and node.id not in seen:
                seen.append(node.id)
        return seen

    def evaluate(self, env: dict):
        def ev(n):
            if isinstance(n, ast.Expression):
                return ev(n.body)
            if isinstance(n, ast.Constant):
                return n.value
            if isinstance(n, ast.Name):
                if n.id not in env:
                    raise FormulaError(f"missing value for {n.id!r}")
                return env[n.id]
            if isinstance(n, ast.UnaryOp):
                v = ev(n.operand)
                return -v if isinstance(n.op, ast.USub) else v
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            return a / b
        return ev(self.tree)

    def roles(self) -> dict[str, VarRole]:
        """Role & polarity of each variable, assuming all metric values are positive."""
        out: dict[str, VarRole] = {}
        top_is_sum = isinstance(self.tree.body, ast.BinOp) and isinstance(self.tree.body.op, (ast.Add, ast.Sub))

        def walk(n, sign: int, in_div: bool, in_sum: bool):
            if isinstance(n, ast.Name):
                pol = sign * (-1 if in_div else 1)
                if in_sum and not in_div and top_is_sum and _is_direct_term(self.tree.body, n):
                    role = "addend" if pol > 0 else "subtrahend"
                else:
                    role = "divisor" if in_div else "factor"
                out.setdefault(n.id, VarRole(role, pol))
            elif isinstance(n, ast.UnaryOp):
                walk(n.operand, -sign if isinstance(n.op, ast.USub) else sign, in_div, in_sum)
            elif isinstance(n, ast.BinOp):
                if isinstance(n.op, (ast.Add, ast.Sub)):
                    walk(n.left, sign, in_div, True)
                    walk(n.right, -sign if isinstance(n.op, ast.Sub) else sign, in_div, True)
                elif isinstance(n.op, ast.Mult):
                    walk(n.left, sign, in_div, in_sum)
                    walk(n.right, sign, in_div, in_sum)
                else:  # Div
                    walk(n.left, sign, in_div, in_sum)
                    walk(n.right, sign, not in_div, in_sum)

        walk(self.tree.body, +1, False, False)
        return out

    def is_additive(self) -> bool:
        return all(r.role in ("addend", "subtrahend") for r in self.roles().values())

    def is_multiplicative(self) -> bool:
        return all(r.role in ("factor", "divisor") for r in self.roles().values()) and not any(
            isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub)) for n in ast.walk(self.tree))


def _is_direct_term(body: ast.AST, name: ast.Name) -> bool:
    """True if ``name`` is a direct +/- term of the top-level sum (not nested in a product)."""
    stack = [body]
    while stack:
        n = stack.pop()
        if n is name:
            return True
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub)):
            stack += [n.left, n.right]
        elif isinstance(n, ast.UnaryOp):
            stack.append(n.operand)
    return False


# ---------------------------------------------------------------- units
DIMENSIONLESS = {"ratio", "1", "pct", "share", ""}


@dataclass(frozen=True)
class Unit:
    exps: tuple  # sorted ((base, Fraction), ...)

    @classmethod
    def parse(cls, s: str) -> "Unit":
        s = (s or "").strip()
        num, _, den = s.partition("/")
        exps: dict[str, Fraction] = {}
        for part, sgn in ((num, 1), (den, -1)):
            for tok in part.replace("*", " ").split():
                if tok in DIMENSIONLESS:
                    continue
                exps[tok] = exps.get(tok, Fraction(0)) + sgn
        return cls(tuple(sorted((k, v) for k, v in exps.items() if v != 0)))

    def __mul__(self, o: "Unit") -> "Unit":
        d = dict(self.exps)
        for k, v in o.exps:
            d[k] = d.get(k, Fraction(0)) + v
        return Unit(tuple(sorted((k, v) for k, v in d.items() if v != 0)))

    def inverse(self) -> "Unit":
        return Unit(tuple((k, -v) for k, v in self.exps))

    def __str__(self) -> str:
        if not self.exps:
            return "ratio"
        num = [k if v == 1 else f"{k}^{v}" for k, v in self.exps if v > 0]
        den = [k if v == -1 else f"{k}^{-v}" for k, v in self.exps if v < 0]
        return (" ".join(num) or "1") + ("/" + " ".join(den) if den else "")


def infer_unit(formula: Formula, units: dict[str, str]) -> Unit:
    """Infer the unit of a formula; raises FormulaError on adding incompatible units."""
    def ev(n) -> Unit | None:  # None = numeric constant (adapts to anything)
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return None
        if isinstance(n, ast.Name):
            return Unit.parse(units[n.id])
        if isinstance(n, ast.UnaryOp):
            return ev(n.operand)
        a, b = ev(n.left), ev(n.right)
        if isinstance(n.op, (ast.Add, ast.Sub)):
            if a is not None and b is not None and a != b:
                raise FormulaError(f"cannot add/subtract {a} and {b} in {formula.expr!r}")
            return a if a is not None else b
        a, b = a or Unit(()), b or Unit(())
        return a * b if isinstance(n.op, ast.Mult) else a * b.inverse()
    return ev(formula.tree) or Unit(())
