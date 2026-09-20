from gaohe import cli


def write_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"DATA_DIR={tmp_path / 'data'}\nLLM_API_KEY=top-secret-value\n",
        encoding="utf-8",
    )
    return env_file


def test_source_add_and_list_use_the_configured_data_directory_without_printing_keys(tmp_path, capsys):
    env_file = write_env(tmp_path)

    assert cli.main([
        "source", "add", "--name", "Example News", "--feed-url", "https://example.test/feed.xml",
        "--article-url", "https://example.test/news", "--env-file", str(env_file),
    ]) == 0
    assert cli.main(["source", "list", "--env-file", str(env_file)]) == 0

    output = capsys.readouterr().out
    assert "id=1 enabled=yes name=Example News feed_url=https://example.test/feed.xml article_url=https://example.test/news" in output
    assert "top-secret-value" not in output


def test_source_list_redacts_url_credentials_and_sensitive_query_values(tmp_path, capsys):
    env_file = write_env(tmp_path)

    assert cli.main([
        "source", "add", "--name", "Private Feed",
        "--feed-url", "https://token:secret@example.test/feed.xml?token=feed-token&lang=zh-TW",
        "--article-url", "https://reader:article-secret@example.test/news?api_key=article-key&page=2",
        "--env-file", str(env_file),
    ]) == 0
    assert cli.main(["source", "list", "--env-file", str(env_file)]) == 0

    output = capsys.readouterr().out
    for secret in ("token:secret", "feed-token", "reader:article-secret", "article-key"):
        assert secret not in output
    assert "https://example.test/feed.xml?token=%2A%2A%2A&lang=zh-TW" in output
    assert "https://example.test/news?api_key=%2A%2A%2A&page=2" in output


def test_source_enable_and_disable_change_the_persisted_source_state(tmp_path, capsys):
    env_file = write_env(tmp_path)
    assert cli.main([
        "source", "add", "--name", "Example", "--feed-url", "https://example.test/feed", "--env-file", str(env_file),
    ]) == 0

    assert cli.main(["source", "disable", "--id", "1", "--env-file", str(env_file)]) == 0
    assert cli.main(["source", "list", "--env-file", str(env_file)]) == 0
    assert "enabled=no" in capsys.readouterr().out

    assert cli.main(["source", "enable", "--id", "1", "--env-file", str(env_file)]) == 0
    assert cli.main(["source", "list", "--env-file", str(env_file)]) == 0
    assert "enabled=yes" in capsys.readouterr().out


def test_source_add_rejects_blank_names_and_non_http_urls(tmp_path, capsys):
    env_file = write_env(tmp_path)

    assert cli.main([
        "source", "add", "--name", " ", "--feed-url", "https://example.test/feed", "--env-file", str(env_file),
    ]) == 2
    assert "--name must not be empty" in capsys.readouterr().err

    assert cli.main([
        "source", "add", "--name", "Example", "--feed-url", "file:///tmp/feed.xml", "--env-file", str(env_file),
    ]) == 2
    assert "--feed-url must be an HTTP(S) URL" in capsys.readouterr().err

    assert cli.main([
        "source", "add", "--name", "Example", "--feed-url", "https://example.test/feed",
        "--article-url", "ftp://example.test/news", "--env-file", str(env_file),
    ]) == 2
    assert "--article-url must be an HTTP(S) URL" in capsys.readouterr().err
