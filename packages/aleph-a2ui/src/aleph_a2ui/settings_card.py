"""Generate a plugin's settings surface from its config schema.

Every plugin declares its configuration as JSON Schema. This module turns that
declaration into an A2UI surface — no plugin-authored React, no model call, no
build step. It is a pure function of the schema and the current values, so the
same input always produces the same screen and the whole thing is unit-testable
without a browser, a database or a gateway.

That matters more than convenience. A workbench whose abilities are added at
runtime cannot ship a hand-written settings page per ability: the page would
have to exist before the ability did. Generating it means a plugin an agent
wrote a minute ago gets a settings screen on the same terms as a core one, and
gets it without shipping any code that runs in the browser — which is what makes
it safe to offer at the lowest trust tier.

Only basic-catalog primitives are emitted (`TextField`, `CheckBox`,
`ChoicePicker`, `Slider`, `DateTimeInput`, plus `Text`/`Column`/`Divider`/
`Button`), so nothing here depends on Aleph's own card impls and nothing needs
registering before it will render.

**A field whose schema this module cannot render is stated, not skipped.** An
unsupported type emits a visible line naming the field and the reason. Silently
dropping it would produce a settings screen that looks complete and quietly
cannot configure the thing — the failure mode this codebase has paid for
repeatedly.
"""

from __future__ import annotations

from typing import Any

from aleph_a2ui.messages import full_surface


class SecretFieldRefused(ValueError):
    """A settings schema declared a field that must not be rendered as one.

    Raised rather than skipped. Skipping would produce a settings screen missing
    a field the plugin author expected to see, with no explanation — they would
    add it back under a different name, which is worse than the original
    mistake because the router's key-name redaction would then miss it too.
    """


__all__ = [
    "SETTINGS_SAVE_ACTION",
    "SETTINGS_VALUE_PREFIX",
    "SecretFieldRefused",
    "settings_components",
    "settings_surface",
    "submitted_values",
]

SETTINGS_SAVE_ACTION = "plugin.settings.save"

#: Every submitted value is carried under this prefix in the save action's
#: context.
#:
#: An A2UI action context is a FLAT object — `DynamicValue` admits strings,
#: numbers, booleans, arrays and bindings, but not a nested object of bindings,
#: so the values cannot be grouped under a key. Without a prefix a plugin whose
#: config declares a field called `plugin_id` silently overwrites the only key
#: the server uses to decide WHICH plugin it is saving, and the save lands on
#: another plugin with nothing raised. The prefix makes the two namespaces
#: disjoint by construction rather than by hoping no plugin picks the name.
SETTINGS_VALUE_PREFIX = "field:"

_LONG_TEXT_MIN = 200


def _humanize(key: str) -> str:
    """`max_concurrent_runs` -> `Max concurrent runs`."""
    words = key.replace("-", " ").replace("_", " ").split()
    if not words:
        return key
    return " ".join([words[0].capitalize(), *words[1:]])


def _label(key: str, field: dict[str, Any]) -> str:
    title = field.get("title")
    return title if isinstance(title, str) and title else _humanize(key)


def _field_component(*, cid: str, key: str, field: dict[str, Any]) -> dict[str, Any]:
    """Map one JSON-Schema property onto one basic-catalog component.

    The returned dict is a complete `updateComponents` entry: `id`, `component`,
    and every prop inline. `value` is always a data-model binding so the field
    both displays the current setting and writes edits back to the same path.
    """
    label = _label(key, field)
    bind: dict[str, str] = {"path": f"/{key}"}
    ftype = field.get("type")
    fmt = field.get("format")
    enum = field.get("enum")

    if ftype == "boolean":
        return {"id": cid, "component": "CheckBox", "label": label, "value": bind}

    if isinstance(enum, list) and enum:
        return {
            "id": cid,
            "component": "ChoicePicker",
            "label": label,
            "variant": "mutuallyExclusive",
            "displayStyle": "chips",
            "options": [{"label": str(v), "value": str(v)} for v in enum],
            "value": bind,
        }

    if ftype == "array":
        items = field.get("items")
        item_enum = items.get("enum") if isinstance(items, dict) else None
        if isinstance(item_enum, list) and item_enum:
            return {
                "id": cid,
                "component": "ChoicePicker",
                "label": label,
                "variant": "multipleSelection",
                "displayStyle": "checkbox",
                "options": [{"label": str(v), "value": str(v)} for v in item_enum],
                "value": bind,
            }
        return _unsupported(cid=cid, key=key, reason="a list with no fixed set of choices")

    if ftype in ("integer", "number"):
        lo, hi = field.get("minimum"), field.get("maximum")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            return {
                "id": cid,
                "component": "Slider",
                "label": label,
                "min": lo,
                "max": hi,
                "value": bind,
            }
        return {
            "id": cid,
            "component": "TextField",
            "label": label,
            "variant": "number",
            "value": bind,
        }

    if ftype == "string":
        if fmt in ("date", "date-time"):
            return {
                "id": cid,
                "component": "DateTimeInput",
                "label": label,
                "enableDate": True,
                "enableTime": fmt == "date-time",
                "value": bind,
            }
        if fmt == "password" or field.get("writeOnly") is True:
            # REFUSED, not obscured.
            #
            # `variant: "obscured"` hides the value on screen and changes
            # nothing about where it goes: a settings field's value travels in
            # the action context, and `ActionRouter.dispatch` persists params
            # AND the result to `card_actions` and to the hash-chained ledger.
            # Both are append-only. Obscured on screen, permanent in two tables.
            #
            # `redact_secrets` in the router is the backstop and it matches on
            # key NAME, so a field called `token_for_service` is caught and one
            # called `x` is not. This is the half that cannot be fooled by a
            # name: the schema SAID it was a secret.
            #
            # Secrets belong in `ConnectorCredential`, which encrypts them.
            msg = (
                f"settings field {key!r} declares itself a secret "
                f"({'format: password' if fmt == 'password' else 'writeOnly: true'}), "
                "and a settings value is persisted in plaintext to card_actions "
                "and to the append-only ledger. Store it as a ConnectorCredential "
                "instead — that path encrypts."
            )
            raise SecretFieldRefused(msg)
        elif fmt == "textarea":
            variant = "longText"
        else:
            max_len = field.get("maxLength")
            long_by_length = isinstance(max_len, int) and max_len >= _LONG_TEXT_MIN
            variant = "longText" if long_by_length else "shortText"
        comp: dict[str, Any] = {
            "id": cid,
            "component": "TextField",
            "label": label,
            "variant": variant,
            "value": bind,
        }
        pattern = field.get("pattern")
        if isinstance(pattern, str) and pattern:
            comp["validationRegexp"] = pattern
        return comp

    return _unsupported(cid=cid, key=key, reason=f"type {ftype!r}")


def _unsupported(*, cid: str, key: str, reason: str) -> dict[str, Any]:
    """A visible, honest placeholder for a field this module cannot render."""
    return {
        "id": cid,
        "component": "Text",
        "variant": "body",
        "text": f"⚠ {_humanize(key)} — not editable here ({reason}).",
    }


def settings_components(
    *,
    plugin_title: str,
    config_schema: dict[str, Any],
    plugin_id: str,
    plugin_kind: str = "plugin",
    description: str | None = None,
) -> list[dict[str, Any]]:
    """Build the component list for a plugin's settings surface.

    The root is a `Column` whose id is `root` (what `A2uiSurface` mounts).
    """
    props = config_schema.get("properties")
    properties: dict[str, Any] = props if isinstance(props, dict) else {}

    components: list[dict[str, Any]] = []
    children: list[str] = []

    components.append({"id": "title", "component": "Text", "variant": "h3", "text": plugin_title})
    children.append("title")

    if description:
        components.append(
            {"id": "subtitle", "component": "Text", "variant": "caption", "text": description}
        )
        children.append("subtitle")

    components.append({"id": "rule-top", "component": "Divider", "axis": "horizontal"})
    children.append("rule-top")

    # `context` carries every field as a path binding, so the button hands the
    # server the values as they stand at click time. No client code involved.
    # `plugin_kind` rides along because the server has more than one kind of
    # thing with settings and the id alone does not say which table to write.
    context: dict[str, Any] = {"plugin_id": plugin_id, "plugin_kind": plugin_kind}

    for index, (key, raw) in enumerate(properties.items()):
        field: dict[str, Any] = raw if isinstance(raw, dict) else {}
        cid = f"f{index}-{key}"
        components.append(_field_component(cid=cid, key=key, field=field))
        children.append(cid)

        help_text = field.get("description")
        if isinstance(help_text, str) and help_text:
            help_id = f"{cid}-help"
            components.append(
                {"id": help_id, "component": "Text", "variant": "caption", "text": help_text}
            )
            children.append(help_id)

        context[f"{SETTINGS_VALUE_PREFIX}{key}"] = {"path": f"/{key}"}

    components.append({"id": "rule-bottom", "component": "Divider", "axis": "horizontal"})
    children.append("rule-bottom")

    components.append({"id": "save-label", "component": "Text", "variant": "body", "text": "Save"})
    components.append(
        {
            "id": "save",
            "component": "Button",
            "variant": "primary",
            "child": "save-label",
            "action": {"event": {"name": SETTINGS_SAVE_ACTION, "context": context}},
        }
    )
    children.append("save")

    components.append(
        {"id": "root", "component": "Column", "children": children, "align": "stretch"}
    )
    return components


def settings_data_model(
    *, config_schema: dict[str, Any], current: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Seed every bound path: the stored value, else the schema default, else empty.

    Every property gets a key even when unset, so a binding never resolves
    against a missing path.
    """
    props = config_schema.get("properties")
    properties: dict[str, Any] = props if isinstance(props, dict) else {}
    values: dict[str, Any] = {}
    stored: dict[str, Any] = current or {}

    for key, raw in properties.items():
        field: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if key in stored:
            values[key] = stored[key]
        elif "default" in field:
            values[key] = field["default"]
        elif field.get("type") == "boolean":
            values[key] = False
        elif field.get("type") == "array":
            values[key] = []
        elif field.get("type") in ("integer", "number"):
            values[key] = field.get("minimum", 0)
        else:
            values[key] = ""
    return values


def settings_surface(
    *,
    plugin_id: str,
    plugin_title: str,
    config_schema: dict[str, Any],
    catalog_id: str,
    plugin_kind: str = "plugin",
    surface_id: str | None = None,
    current: dict[str, Any] | None = None,
    description: str | None = None,
) -> list[dict[str, Any]]:
    """The ordered A2UI message list rendering one plugin's settings screen."""
    return full_surface(
        surface_id=surface_id or f"settings:{plugin_id}",
        catalog_id=catalog_id,
        components=settings_components(
            plugin_title=plugin_title,
            config_schema=config_schema,
            plugin_id=plugin_id,
            plugin_kind=plugin_kind,
            description=description,
        ),
        data_model=settings_data_model(config_schema=config_schema, current=current),
    )


def submitted_values(params: dict[str, Any]) -> dict[str, Any]:
    """Pull the settings values back out of a `plugin.settings.save` context.

    The inverse of the `field:` prefixing done above, and the only place that
    inverse is written — a handler doing its own `k.split(":")` would be a
    second, silently divergent reader of the same convention.
    """
    return {
        key[len(SETTINGS_VALUE_PREFIX) :]: value
        for key, value in params.items()
        if key.startswith(SETTINGS_VALUE_PREFIX)
    }
