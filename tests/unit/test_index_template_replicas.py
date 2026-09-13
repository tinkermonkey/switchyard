"""
Every index template this codebase defines must pin number_of_replicas to 0.

The deployment is single-node by construction -- docker-compose.yml defines one
elasticsearch service -- so a replica shard can never be allocated. That is not
merely wasteful, it JAMS retention:

    phase=warm action=migrate step=check-migration age=68.27d
    step_info: 'Waiting for all shard copies to be active'
               all_shards_active: False, number_of_replicas: 1

The warm phase's migrate action waits for all shard copies to be active. On one
node that is unsatisfiable and there is no timeout, so the index never reaches
the delete phase and the 30-day policy silently stops applying. Both OTEL
backing indices sat there for 68 days holding data every other index had long
since aged out, while the cluster read yellow.

The two OTEL templates were the only ones that omitted it, because they compose
Elastic's built-in logs@settings / metrics@tsdb-settings, which default it to 1.
A template that says nothing inherits the jam.

So this asserts the property across ALL templates rather than the two that were
wrong -- the next template composed from a built-in is the one that would
otherwise repeat it.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

import services.pattern_detection_schema as schema


def _templates():
    """Every dict in the schema module that looks like an index template."""
    found = {}
    for name in dir(schema):
        value = getattr(schema, name)
        if not isinstance(value, dict):
            continue
        if 'index_patterns' not in value and 'template' not in value:
            continue
        if not isinstance(value.get('template'), dict):
            continue
        found[name] = value
    return found


def test_the_discovery_finds_the_templates_it_is_meant_to():
    """A test that iterates over a collection passes trivially if the
    collection is empty."""
    names = _templates()

    assert 'CLAUDE_OTEL_LOGS_TEMPLATE' in names
    assert 'CLAUDE_OTEL_METRICS_TEMPLATE' in names
    assert len(names) >= 5, f"only found {sorted(names)}"


@pytest.mark.parametrize('name', sorted(_templates()))
def test_every_template_pins_replicas_to_zero(name):
    template = _templates()[name]
    settings = template['template'].get('settings', {})
    index = settings.get('index', settings)

    assert 'number_of_replicas' in index, (
        f"{name} does not pin number_of_replicas. Composed templates inherit 1 "
        f"from Elastic's built-ins, and an unallocatable replica jams ILM at "
        f"warm/migrate/check-migration forever -- retention stops applying and "
        f"nothing reports it."
    )
    assert index['number_of_replicas'] == 0, (
        f"{name} asks for {index['number_of_replicas']} replicas; this "
        f"deployment has one Elasticsearch node, so they can never allocate."
    )


class TestTheOtelTemplatesSpecifically:
    """These are the two that were wrong, and the reason they were wrong --
    composed_of pulling in a built-in default -- is still present."""

    @pytest.mark.parametrize('name,builtin', [
        ('CLAUDE_OTEL_LOGS_TEMPLATE', 'logs@settings'),
        ('CLAUDE_OTEL_METRICS_TEMPLATE', 'metrics@tsdb-settings'),
    ])
    def test_the_override_still_sits_above_the_builtin(self, name, builtin):
        template = getattr(schema, name)

        assert builtin in template['composed_of'], (
            "precondition: this test is about overriding that built-in"
        )
        assert template['template']['settings']['index']['number_of_replicas'] == 0
