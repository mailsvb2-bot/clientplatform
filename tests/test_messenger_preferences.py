import importlib


def test_preference_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv('CLIENTPLATFORM_DB_PATH', str(tmp_path / 'test.db'))

    core_paths = importlib.import_module('core.paths')
    db_core = importlib.import_module('services.db.core')
    schema_core = importlib.import_module('services.schema_core')
    prefs = importlib.import_module('services.messenger.preferences')
    schema = importlib.import_module('services.schema')

    importlib.reload(core_paths)
    importlib.reload(db_core)
    importlib.reload(schema_core)
    importlib.reload(prefs)
    importlib.reload(schema)

    schema.init_db()

    prefs.record_channel_identity(1, 'telegram', '1', username='u1')
    prefs.record_channel_identity(1, 'vk', 'vk1')
    prefs.set_preferred_platform(1, 'vk')

    assert prefs.get_preferred_platform(1) == 'vk'
    assert prefs.resolve_delivery_platform(1) == 'vk'
    assert set(prefs.get_available_platforms(1)) == {'vk', 'telegram'}
