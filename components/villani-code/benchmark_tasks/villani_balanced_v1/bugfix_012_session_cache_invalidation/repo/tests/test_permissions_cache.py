from app.store import UserStore

def test_role_update_invalidates_cached_permissions():
    store = UserStore()
    store.set_role('u1', 'guest')
    assert store.permissions_for('u1') == ['read']
    store.set_role('u1', 'admin')
    assert store.permissions_for('u1') == ['read', 'write', 'delete']

def test_second_user_cache_is_isolated():
    store = UserStore()
    store.set_role('u1', 'editor')
    store.set_role('u2', 'guest')
    assert store.permissions_for('u1') == ['read', 'write']
    assert store.permissions_for('u2') == ['read']
