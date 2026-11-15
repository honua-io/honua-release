#!/usr/bin/env python3
"""What a published image must be before the trunk resolver pins it.

A component's `imageLabels` state the published variant. It is proved from each platform's
image config labels; a tag name or tag order never decides it.
"""
from __future__ import annotations

from dataclasses import dataclass


class ImageLabelRuleError(ValueError):
    """`imageLabels` is not a usable rule; no image may be bound under it."""


def label_rule(rule):
    """A component's `imageLabels` as (required, whenDeclared) label maps.

    `required` labels must be declared with the stated value. `whenDeclared` labels may be
    absent (images built before the label existed), but a declared value must match.
    """
    if rule is None:
        return {}, {}
    if (not isinstance(rule, dict) or not rule or set(rule) - {'required', 'whenDeclared'}
            or not all(_label_values(part) for part in rule.values())):
        raise ImageLabelRuleError(
            'imageLabels must hold non-empty string maps under required and/or whenDeclared')
    required, when_declared = rule.get('required', {}), rule.get('whenDeclared', {})
    if set(required) & set(when_declared):
        raise ImageLabelRuleError('imageLabels names a label under both required and whenDeclared')
    return required, when_declared


def _label_values(part):
    return isinstance(part, dict) and bool(part) and all(
        isinstance(item, str) and item for pair in part.items() for item in pair)


@dataclass(frozen=True)
class ImageRequirement:
    sha: str
    architectures: tuple
    required: dict
    when_declared: dict

    @classmethod
    def of(cls, sha, architectures, labels=None):
        return cls(sha, tuple(architectures), *label_rule(labels))

    def broken_label(self, labels):
        """The first label rule these config labels break, or None."""
        for name, wanted in sorted(self.required.items()):
            if labels.get(name) is None:
                return f'declares no {name} label; this component requires {wanted}'
            if labels[name] != wanted:
                return f'declares {name}={labels[name]}; this component requires {wanted}'
        for name, wanted in sorted(self.when_declared.items()):
            if labels.get(name) not in (None, wanted):
                return f'declares {name}={labels[name]}; this component requires {wanted}'
        return None
