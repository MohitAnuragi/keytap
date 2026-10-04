"""
Checks for `assert` steps. One function per check. Everything else is generic and written once:
waiting until the check holds, the timeout, soft failures, the failure message and the failure
bundle. The validator, `suite.py checks`, `suite.py check` and the Claude skill read this registry.

To add a check, write a function and decorate it. Nothing else changes:

    @check("focused")                       # needs=("selector",) is the default
    def focused(ctx, a):
        '''The element has input focus.'''
        el = ctx.find(a.selector)
        if el is None:
            return False, "not on screen"
        return bool(el.info.get("focused")), el.info.get("focused")

A check gets a Context and the action (fields: selector, value, soft, ...) and returns
(ok, actual). `actual` is what was found; it goes into the failure message. `needs` lists the
action fields the step must have.
"""

import lookup

CHECKS = {}


def check(name, needs=("selector",)):
    def register(fn):
        CHECKS[name] = {"fn": fn, "needs": tuple(needs), "doc": " ".join((fn.__doc__ or "").split())}
        return fn
    return register


class Context:
    """What a check can look at. dev is the uiautomator2 device."""

    def __init__(self, dev, serial=None):
        self.dev = dev
        self.serial = serial

    def find(self, selector):
        return lookup.find_element(self.dev, selector)

    def count(self, selector):
        return lookup.count_elements(self.dev, selector)

    def foreground(self):
        return lookup.foreground(self.serial)


def describe():
    """One line per check: name, the fields it needs, what it does."""
    return [f"{name:<14} needs {', '.join(c['needs']):<16} {c['doc']}" for name, c in CHECKS.items()]


@check("visible")
def visible(ctx, a):
    """The element is on screen."""
    return ctx.find(a.selector) is not None, "not on screen"


@check("gone")
def gone(ctx, a):
    """The element is not on screen. Passes at once if it never appeared, so pair it with an earlier visible."""
    return ctx.find(a.selector) is None, "still on screen"


@check("text_equals", needs=("selector", "value"))
def text_equals(ctx, a):
    """The element's text equals value."""
    el = ctx.find(a.selector)
    text = el.info.get("text") if el is not None else "element not on screen"
    return el is not None and text == str(a.value), text


@check("text_contains", needs=("selector", "value"))
def text_contains(ctx, a):
    """The element's text contains value."""
    el = ctx.find(a.selector)
    text = el.info.get("text") if el is not None else "element not on screen"
    return el is not None and str(a.value) in (text or ""), text


@check("enabled")
def enabled(ctx, a):
    """The element is on screen and enabled."""
    el = ctx.find(a.selector)
    return el is not None and bool(el.info.get("enabled")), "not on screen" if el is None else "disabled"


@check("disabled")
def disabled(ctx, a):
    """The element is on screen and disabled."""
    el = ctx.find(a.selector)
    return el is not None and not el.info.get("enabled"), "not on screen" if el is None else "enabled"


@check("count", needs=("selector", "value"))
def count(ctx, a):
    """At least value elements match the selector (a list has at least that many rows)."""
    n = ctx.count(a.selector)
    return n >= int(a.value), n


@check("screen", needs=("value",))
def screen(ctx, a):
    """The focused screen is package/Activity and contains value (for example MessagesActivity)."""
    pkg, activity = ctx.foreground()
    here = f"{pkg}/{activity}"
    return str(a.value) in here, here
