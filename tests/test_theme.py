import app_theme_qt
import theme


def test_valid_theme_accepts_known_themes():
    assert theme.valid_theme("dark") == "dark"
    assert theme.valid_theme("light") == "light"


def test_valid_theme_falls_back_to_default():
    assert theme.valid_theme("neon") == theme.DEFAULT_THEME
    assert theme.valid_theme(None) == theme.DEFAULT_THEME


def test_get_theme_falls_back_to_default():
    assert theme.get_theme("neon") == theme.THEMES[theme.DEFAULT_THEME]


def test_light_and_dark_define_same_keys():
    assert set(theme.get_theme("dark")) == set(theme.get_theme("light"))


def test_shared_stylesheet_substitutes_all_placeholders():
    for name in ("dark", "light"):
        stylesheet = app_theme_qt.build_stylesheet(name)
        assert "$" not in stylesheet
        assert "QFrame#card" in stylesheet
        assert "QPushButton#primaryBtn" in stylesheet
        assert "QPlainTextEdit" in stylesheet
