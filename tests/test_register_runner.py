from app.services.register import runner as runner_module


def test_extract_js_urls_from_html_supports_regex_fallback():
    html = """
    <html>
      <head>
        <link rel="preload" href="/_next/static/chunks/app-123.js" as="script" />
      </head>
      <body>
        <script>self.__next_f.push(["/_next/static/chunks/main-456.js"])</script>
      </body>
    </html>
    """

    urls = runner_module._extract_js_urls_from_html("https://accounts.x.ai/sign-up", html)

    assert "https://accounts.x.ai/_next/static/chunks/app-123.js" in urls
    assert "https://accounts.x.ai/_next/static/chunks/main-456.js" in urls


def test_extract_action_id_from_text_prefers_supported_prefix():
    text = 'something "next-action":"7f1234567890abcdef1234567890abcdef123456" end'

    action_id = runner_module._extract_action_id_from_text(text)

    assert action_id == "7f1234567890abcdef1234567890abcdef123456"
