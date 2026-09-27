"""Native desktop snapshot contract, independently of model dispatch."""
import copy
import json

import pytest

from management.desktop_state import ThreadSnapshot


def event(change, *, thread='thread-1', host='local', owner='owner-1', version=11):
    return {'type': 'broadcast', 'method': 'thread-stream-state-changed',
            'sourceClientId': owner, 'targetClientIds': ['management-1'],
            'version': version, 'params': {'conversationId': thread,
                'hostId': host, 'change': change}}


def state(**fields):
    return {'id': 'thread-1', 'hostId': 'local', 'turns': [], 'requests': [], **fields}


def snapshot(revision=1, value=None, **routing):
    return event({'type': 'snapshot', 'revision': revision,
                  'conversationState': state() if value is None else value}, **routing)


def patches(items, *, base=1, revision=2, **routing):
    return event({'type': 'patches', 'baseRevision': base,
                  'revision': revision, 'patches': items}, **routing)


def projection(**bounds):
    return ThreadSnapshot('thread-1', 'local', 'owner-1', **bounds)


def ready(value=None, **bounds):
    result = projection(**bounds)
    assert result.accept(snapshot(value=value)) == 'accepted'
    return result


def test_initial_snapshot_matches_real_desktop_schema_and_copies_both_directions():
    incoming = snapshot(value=state(title='Synthetic', threadRuntimeStatus={'type': 'idle'}))
    before = copy.deepcopy(incoming)
    stream = projection()
    assert stream.needs_snapshot and stream.revision is None and stream.get_state() is None
    assert stream.accept(incoming) == 'accepted'
    assert stream.revision == 1 and not stream.needs_snapshot
    incoming['params']['change']['conversationState']['title'] = 'caller changed'
    returned = stream.get_state()
    returned['turns'].append({'turnId': 'caller-inserted'})
    assert stream.get_state() == before['params']['change']['conversationState']


def test_contiguous_immer_patch_array_tracks_real_turn_and_receipt_identity():
    stream = ready()
    first = patches([{'op': 'add', 'path': ['turns', 0], 'value': {
        'turnId': 'turn-1', 'status': 'inProgress',
        'params': {'clientUserMessageId': 'operation-1'}, 'items': []}}])
    before = copy.deepcopy(first)
    assert stream.accept(first) == 'accepted'
    second = patches([
        {'op': 'replace', 'path': ['turns', 0, 'status'], 'value': 'completed'},
        {'op': 'add', 'path': ['turns', 0, 'items', '-'], 'value': {
            'type': 'agentMessage', 'text': 'Synthetic result'}},
    ], base=2, revision=3)
    assert stream.accept(second) == 'accepted'
    assert first == before
    assert stream.revision == 3
    turn = stream.get_state()['turns'][0]
    assert (turn['turnId'], turn['status'], turn['params']['clientUserMessageId']) == (
        'turn-1', 'completed', 'operation-1')
    assert turn['items'][0]['text'] == 'Synthetic result'


def test_dictionary_assign_delete_and_numeric_keys_follow_immer_semantics():
    stream = ready(state(meta={'old': 1, '0': 'zero'}))
    assert stream.accept(patches([
        {'op': 'add', 'path': ['meta', 'old'], 'value': 2},
        {'op': 'replace', 'path': ['meta', 'new'], 'value': 3},
        {'op': 'replace', 'path': ['meta', 0], 'value': 'updated'},
        {'op': 'remove', 'path': ['meta', 'old']},
        {'op': 'remove', 'path': ['meta', 'already-absent']},
    ])) == 'accepted'
    assert stream.get_state()['meta'] == {'new': 3, '0': 'updated'}


def test_list_add_inserts_remove_splices_and_replace_assigns():
    stream = ready(state(values=['a', 'c']))
    assert stream.accept(patches([
        {'op': 'add', 'path': ['values', 1], 'value': 'b'},
        {'op': 'remove', 'path': ['values', 0]},
        {'op': 'replace', 'path': ['values', 1], 'value': 'd'},
        {'op': 'add', 'path': ['values', 2], 'value': 'e'},
    ])) == 'accepted'
    assert stream.get_state()['values'] == ['b', 'd', 'e']


def test_last_root_replace_supersedes_prior_paths_as_in_bundled_immer():
    stream = ready(state(values=['old']))
    payload = patches([
        {'op': 'replace', 'path': ['absent', 'unused'], 'value': 'superseded'},
        {'op': 'replace', 'path': [], 'value': state(values=['new'])},
        {'op': 'add', 'path': ['values', '-'], 'value': 'last'},
    ])
    original = copy.deepcopy(payload)
    assert stream.accept(payload) == 'accepted'
    assert stream.get_state()['values'] == ['new', 'last']
    assert payload == original


def test_duplicate_and_stale_events_never_rollback_current_state():
    stream = ready()
    change = patches([{'op': 'add', 'path': ['title'], 'value': 'current'}])
    assert stream.accept(change) == 'accepted'
    for stale in [change, snapshot(1), snapshot(2), patches([], base=0, revision=1)]:
        assert stream.accept(stale) == 'ignored'
    assert stream.revision == 2
    assert stream.get_state()['title'] == 'current'


def test_gap_discards_ready_state_and_requires_new_enough_snapshot():
    stream = ready()
    assert stream.accept(patches([], base=2, revision=3)) == 'resubscribe'
    assert stream.needs_snapshot and stream.revision is None and stream.get_state() is None
    assert stream.accept(snapshot(2)) == 'ignored'
    assert stream.needs_snapshot
    assert stream.accept(patches([], base=3, revision=4)) == 'resubscribe'
    assert stream.accept(snapshot(4, state(title='resynchronized'))) == 'accepted'
    assert stream.accept(patches([{'op': 'remove', 'path': ['title']}],
                                 base=4, revision=5)) == 'accepted'
    assert stream.revision == 5 and 'title' not in stream.get_state()


def test_patch_before_snapshot_does_not_invent_missing_state():
    stream = projection()
    assert stream.accept(patches([], base=0, revision=1)) == 'resubscribe'
    assert stream.get_state() is None
    assert stream.accept(snapshot(1)) == 'accepted'


@pytest.mark.parametrize('routing', [{'thread': 'other'}, {'host': 'remote'},
                                    {'thread': 'other', 'owner': 'other'}])
def test_unrelated_thread_or_host_traffic_is_ignored_without_losing_readiness(routing):
    stream = ready()
    assert stream.accept(snapshot(2, **routing)) == 'ignored'
    assert stream.revision == 1 and not stream.needs_snapshot


def test_changed_owner_requires_rediscovery_and_cannot_rebind_itself():
    stream = ready()
    assert stream.accept(snapshot(2, owner='other-owner')) == 'resubscribe'
    assert stream.needs_snapshot and stream.owner_client_id == 'owner-1'
    assert stream.accept(snapshot(3, owner='other-owner')) == 'resubscribe'
    assert stream.get_state() is None
    rebound = ThreadSnapshot('thread-1', 'local', 'other-owner')
    assert rebound.accept(snapshot(3, owner='other-owner')) == 'accepted'


@pytest.mark.parametrize('revision', [True, -1, 1.0, '1', None, 1 << 53])
def test_snapshot_revision_must_be_exact_nonnegative_safe_integer(revision):
    stream = ready()
    assert stream.accept(snapshot(revision)) == 'resubscribe'
    assert stream.needs_snapshot


@pytest.mark.parametrize('bad', [state(id='other'), state(hostId='remote'), [], 'text'])
def test_snapshot_embedded_identity_cannot_disagree_with_routing(bad):
    stream = ready()
    assert stream.accept(snapshot(2, bad)) == 'resubscribe'
    assert stream.get_state() is None


@pytest.mark.parametrize('bad', [
    {'op': 'replace', 'path': '/turns/0', 'value': 1},
    {'op': 'replace', 'path': ['missing', 'nested'], 'value': 1},
    {'op': 'replace', 'path': ['turns', 0], 'value': 1},
    {'op': 'add', 'path': ['turns', 2], 'value': 1},
    {'op': 'add', 'path': ['turns', -1], 'value': 1},
    {'op': 'add', 'path': ['turns', True], 'value': 1},
    {'op': 'add', 'path': ['turns', '0'], 'value': 1},
    {'op': 'remove', 'path': ['turns', '-']},
    {'op': 'replace', 'path': ['id', 'nested'], 'value': 1},
    {'op': 'copy', 'path': ['title'], 'value': 1},
    {'op': 'replace', 'path': ['title']},
    {'op': 'replace', 'path': ['meta', '__proto__'], 'value': {}},
    {'op': 'replace', 'path': ['constructor'], 'value': {}},
    {'op': 'replace', 'path': ['prototype'], 'value': {}},
    {'op': 'replace', 'path': ['turns', {}], 'value': {}},
    {'op': 'add', 'path': [], 'value': state()},
    {'op': 'remove', 'path': []},
    {'op': 'replace', 'path': [], 'value': state(id='other')},
    {'op': 'replace', 'path': [], 'value': []},
    {'op': 'remove', 'path': ['id']},
    {'op': 'add', 'path': ['hostId'], 'value': 'remote'},
])
def test_illegal_patch_discards_readiness_atomically_without_mutating_input(bad):
    stream = ready(state(title='original', meta={}))
    payload = patches([{'op': 'replace', 'path': ['title'], 'value': 'partial'}, bad])
    before = copy.deepcopy(payload)
    assert stream.accept(payload) == 'resubscribe'
    assert stream.get_state() is None and stream.revision is None
    assert payload == before


@pytest.mark.parametrize('base,revision', [(True, 2), (1, 3), (-1, 0), (None, 2)])
def test_invalid_patch_revision_relationship_fails_closed(base, revision):
    stream = ready()
    result = stream.accept(patches([], base=base, revision=revision))
    # A fully stale revision is harmless even if its obsolete base was invalid.
    assert result == ('ignored' if revision <= 1 else 'resubscribe')


@pytest.mark.parametrize('version', [10, 12, 11.0, None])
def test_unsupported_version_discards_readiness(version):
    stream = ready()
    assert stream.accept(snapshot(2, version=version)) == 'resubscribe'


def test_unknown_change_shape_and_non_json_values_are_rejected():
    for change in [None, {'type': 'unknown', 'revision': 2},
                   {'type': 'patches', 'revision': 2, 'baseRevision': 1, 'patches': {}},
                   {'type': 'snapshot', 'revision': 2, 'conversationState': state(value=float('nan'))},
                   {'type': 'snapshot', 'revision': 2, 'conversationState': state(value=b'bytes')}]:
        stream = ready()
        assert stream.accept(event(change)) == 'resubscribe'
        assert stream.get_state() is None


def test_unrelated_messages_are_ignored():
    stream = ready()
    for message in [None, [], {'type': 'response'}, {'type': 'broadcast', 'method': 'other'},
                    {'type': 'broadcast', 'method': 'thread-stream-state-changed', 'params': None}]:
        assert stream.accept(message) == 'ignored'
    assert not stream.needs_snapshot


def test_byte_limit_is_compact_utf8_json_and_includes_envelope():
    payload = snapshot(value=state(title='中文'))
    size = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf8'))
    assert projection(max_bytes=size).accept(payload) == 'accepted'
    assert projection(max_bytes=size - 1).accept(payload) == 'resubscribe'


def test_result_size_limit_prevents_many_small_updates_growing_state_unbounded():
    stream = ready(max_bytes=500)
    for index in range(10):
        result = stream.accept(patches([{'op': 'add', 'path': ['turns', '-'],
            'value': 'x' * 90}], base=index + 1, revision=index + 2))
        if result == 'resubscribe':
            break
    assert result == 'resubscribe' and stream.get_state() is None


def test_node_depth_patch_count_limits_and_cyclic_payloads():
    assert projection(max_nodes=5).accept(snapshot()) == 'resubscribe'
    assert projection(max_depth=2).accept(snapshot()) == 'resubscribe'
    stream = ready(max_patches=1)
    assert stream.accept(patches([
        {'op': 'add', 'path': ['a'], 'value': 1},
        {'op': 'add', 'path': ['b'], 'value': 2},
    ])) == 'resubscribe'
    cyclic = state()
    cyclic['cycle'] = cyclic
    assert projection().accept(snapshot(value=cyclic)) == 'resubscribe'


def test_shared_python_objects_are_copied_as_independent_json_values():
    value = {'nested': []}
    stream = ready(state(first=value, second=value))
    assert stream.accept(patches([{'op': 'add', 'path': ['first', 'nested', 0],
                                   'value': 'first only'}])) == 'accepted'
    assert stream.get_state()['second'] == {'nested': []}
    assert value == {'nested': []}


@pytest.mark.parametrize('bounds', [{'max_bytes': 0}, {'max_nodes': True},
                                   {'max_depth': 129}, {'max_patches': -1}])
def test_invalid_limits_are_rejected_at_construction(bounds):
    with pytest.raises(ValueError):
        projection(**bounds)
