"""Unit tests for gemini_quota_manager (no live GCP calls)."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

import gemini_quota_manager as gqm


def _auth_headers(project: str = "my-proj") -> dict[str, str]:
    return {"Authorization": "Bearer token", "x-goog-user-project": project}


def _quota_match(**overrides) -> gqm.QuotaMatch:
    defaults = {
        "name": "projects/p/locations/global/services/s/quotaInfos/q",
        "quota_id": "GenerateContentRequestsPerDayPerProjectPerModel",
        "display_name": ("Request limit per model per day for a project in the paid tier"),
        "metric": "generativelanguage.googleapis.com/generate_requests",
        "dimensions": {"model": "gemini-2.5-flash"},
        "reported_value": "500",
    }
    defaults.update(overrides)
    return gqm.QuotaMatch(**defaults)


class TestMatchesFilters:
    def test_default_filter_matches_paid_tier_daily_quota(self) -> None:
        name = "Request limit per model per day for a project in the paid tier 3"
        assert gqm.matches_filters(name, gqm.DEFAULT_FILTERS)

    def test_default_filter_excludes_predict_quota(self) -> None:
        name = "Predict Request limit per model per day for a project in paid tier 3"
        assert not gqm.matches_filters(name, gqm.DEFAULT_FILTERS)

    def test_case_insensitive(self) -> None:
        assert gqm.matches_filters("REQUEST LIMIT per model", ["request limit"])

    def test_all_filters_required(self) -> None:
        name = "Request limit per model per day for a project in the paid tier"
        assert gqm.matches_filters(name, ["request limit", "paid tier"])
        assert not gqm.matches_filters(name, ["request limit", "free tier"])


class TestModelMatching:
    def test_model_from_dimensions_prefers_model_key(self) -> None:
        assert gqm.model_from_dimensions({"model": "gemini-2.5-pro"}) == "gemini-2.5-pro"
        assert gqm.model_from_dimensions({"base_model": "x"}) == "x"
        assert gqm.model_from_dimensions({}) is None

    def test_allowlist_substring_match(self) -> None:
        assert gqm.model_matches_allowlist("gemini-2.5-flash-001", ["gemini-2.5-flash"])
        assert not gqm.model_matches_allowlist("gemini-1.0-pro", gqm.ALLOWED_MODELS)

    @pytest.mark.parametrize(
        ("model", "disabled"),
        [
            ("gemini-2.5-flash-tts", True),
            ("gemini-2.5-flash-live", True),
            ("gemini-2.0-flash-lite", True),
            ("computer-use-exp", True),
            ("foo-exp-preview", True),
            ("gemini-1.5-flash", True),
            ("gemini-2.0-flash", True),
            ("gemini-2.5-flash-lite", True),
            ("gemini-2.5-flash", False),
            ("gemini-2.5-pro", False),
            ("gemini-3-flash", False),
            ("gemini-3-pro", False),
            ("gemini-3.0-flash", False),
            ("gemini-2.5-flash-experimental", False),
            ("gemini-embedding-001", True),
            ("gemini-2.5-flash-native-audio-dialog", True),
            ("gemini-2.5-flash-preview-image", True),
            ("gemini-2.5-pro-1p-freebie", True),
            ("gemini-3.1-flash-image", True),
            ("gemini-3-pro-image", True),
        ],
    )
    def test_model_should_disable(self, model: str, disabled: bool) -> None:
        assert gqm.model_should_disable(model) is disabled

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("gemini-2.5-flash", (2, 5, gqm.TIER_FLASH)),
            ("gemini-2.5-flash-001", (2, 5, gqm.TIER_FLASH)),
            ("gemini-2.5-pro", (2, 5, gqm.TIER_PRO)),
            ("gemini-3-flash", (3, 0, gqm.TIER_FLASH)),
            ("gemini-3-pro", (3, 0, gqm.TIER_PRO)),
            ("gemini-2.5-flash-lite", (2, 5, gqm.TIER_FLASH_LITE)),
            ("gemini-1.5-flash-8b", (1, 5, gqm.TIER_FLASH_LITE)),
        ],
    )
    def test_gemini_model_tier(self, model: str, expected: tuple[int, int, int]) -> None:
        assert gqm.gemini_model_tier(model) == expected

    def test_target_value_for_disabled_model(self) -> None:
        assert gqm.target_value_for_model("gemini-2.5-flash-tts", 100) == 0
        assert gqm.target_value_for_model("gemini-2.5-flash", 100) == 100

    def test_format_reported_value(self) -> None:
        assert gqm.format_reported_value(_quota_match(reported_value="-1")) == "-1 (unlimited)"
        assert gqm.format_reported_value(_quota_match(reported_value="Unlimited")) == "-1 (unlimited)"
        assert gqm.format_reported_value(_quota_match(reported_value="500")) == "500"
        assert gqm.format_reported_value(_quota_match(reported_value="n/a")) == "n/a"

    def test_format_policy(self) -> None:
        assert gqm.format_policy("gemini-2.5-flash", gqm.DEFAULT_CAP_VALUE, _quota_match(reported_value="5000")) == "cap → 1000"
        assert gqm.format_policy("gemini-2.5-flash", gqm.DEFAULT_CAP_VALUE, _quota_match(reported_value="500")) == "leave → 500"
        assert gqm.format_policy("gemini-2.5-flash-tts", 100, _quota_match()) == "disable → 0"
        assert gqm.format_policy("gemini-2.5-pro", 250, _quota_match(reported_value="300")) == "cap → 250"


class TestReportedValue:
    def test_is_unlimited(self) -> None:
        assert gqm.is_unlimited(_quota_match(reported_value="-1"))
        assert gqm.is_unlimited(_quota_match(reported_value="Unlimited"))
        assert not gqm.is_unlimited(_quota_match(reported_value="100"))

    def test_current_numeric_value(self) -> None:
        assert gqm.current_numeric_value(_quota_match(reported_value="250")) == 250.0
        assert gqm.current_numeric_value(_quota_match(reported_value="-1")) is None
        assert gqm.current_numeric_value(_quota_match(reported_value="n/a")) is None


class TestQuotaPreference:
    def test_preference_id_within_limit(self) -> None:
        match = _quota_match(quota_id="short-id", dimensions={"model": "gemini-2.5-flash"})
        pref_id = gqm.build_quota_preference_id(match)
        assert pref_id == "short-id-model-gemini-2.5-flash"
        assert len(pref_id) <= 63

    def test_preference_id_truncated_with_hash(self) -> None:
        match = _quota_match(
            quota_id="a" * 40,
            dimensions={"model": "gemini-2.5-flash", "region": "europe-west1"},
        )
        pref_id = gqm.build_quota_preference_id(match)
        assert len(pref_id) <= 63
        assert pref_id.endswith(pref_id.split("-")[-1])  # hash suffix present

    def test_preference_body(self) -> None:
        match = _quota_match()
        resource_name = gqm.quota_preference_name("my-proj", gqm.build_quota_preference_id(match))
        body = gqm.build_quota_preference_body(resource_name, gqm.DEFAULT_SERVICE, match, 100)
        assert body["name"] == resource_name
        assert body["quotaConfig"]["preferredValue"] == 100
        assert body["quotaId"] == match.quota_id
        assert body["service"] == gqm.DEFAULT_SERVICE

    def test_build_preference_lookup(self) -> None:
        prefs = [
            {
                "name": "projects/p/locations/global/quotaPreferences/existing-id",
                "service": gqm.DEFAULT_SERVICE,
                "quotaId": "daily_rpd",
                "dimensions": {"model": "gemini-2.5-flash"},
            }
        ]
        lookup = gqm.build_preference_lookup(prefs)
        key = gqm.preference_lookup_key(gqm.DEFAULT_SERVICE, "daily_rpd", {"model": "gemini-2.5-flash"})
        assert lookup[key] == "projects/p/locations/global/quotaPreferences/existing-id"

    @patch.object(gqm, "request_with_backoff")
    def test_apply_update_uses_patch_with_validate_only(self, mock_request: MagicMock) -> None:
        mock_request.return_value = MagicMock(status_code=200, text="{}")
        match = _quota_match()
        limiter = gqm.RateLimiter(rate=10.0)
        ok = gqm.apply_update("my-proj", gqm.DEFAULT_SERVICE, _auth_headers(), match, 0, dry_run=True, limiter=limiter, max_retries=1)
        assert ok
        method, url = mock_request.call_args[0][1], mock_request.call_args[0][2]
        assert method == "PATCH"
        assert url.endswith("/quotaPreferences/" + gqm.build_quota_preference_id(match))
        assert mock_request.call_args.kwargs["params"]["validateOnly"] == "true"
        assert mock_request.call_args.kwargs["params"]["allowMissing"] == "true"

    @patch.object(gqm, "request_with_backoff")
    def test_apply_update_reuses_existing_preference_name(self, mock_request: MagicMock) -> None:
        mock_request.return_value = MagicMock(status_code=200, text="{}")
        match = _quota_match()
        lookup = gqm.build_preference_lookup(
            [
                {
                    "name": "projects/my-proj/locations/global/quotaPreferences/console-created-id",
                    "service": gqm.DEFAULT_SERVICE,
                    "quotaId": match.quota_id,
                    "dimensions": match.dimensions,
                }
            ]
        )
        limiter = gqm.RateLimiter(rate=10.0)
        ok = gqm.apply_update(
            "my-proj",
            gqm.DEFAULT_SERVICE,
            _auth_headers(),
            match,
            0,
            dry_run=True,
            limiter=limiter,
            max_retries=1,
            preference_lookup=lookup,
        )
        assert ok
        assert mock_request.call_args[0][2].endswith("/quotaPreferences/console-created-id")
        assert "allowMissing" not in mock_request.call_args.kwargs["params"]


class TestDimensionsInfos:
    def test_prefers_dimensions_infos_api_field(self) -> None:
        info = {
            "dimensionsInfos": [{"dimensions": {"model": "gemini-2.5-flash"}, "details": {"value": "1"}}],
            "dimensionsInfo": [{"dimensions": {"model": "wrong"}, "details": {"value": "2"}}],
        }
        assert len(gqm.dimensions_infos_from_quota(info)) == 1
        assert gqm.dimensions_infos_from_quota(info)[0]["dimensions"]["model"] == "gemini-2.5-flash"


class TestFindMatchingQuotas:
    @patch.object(gqm, "list_quota_infos")
    def test_filters_by_display_name_and_model(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            {
                "name": "n1",
                "quotaId": "daily_rpd",
                "quotaDisplayName": ("Request limit per model per day for a project in the paid tier 3"),
                "metric": "m1",
                "dimensionsInfos": [
                    {
                        "dimensions": {"model": "gemini-2.5-flash"},
                        "details": {"value": "500"},
                    },
                    {
                        "dimensions": {"model": "gemini-1.0-pro"},
                        "details": {"value": "100"},
                    },
                    {
                        "dimensions": {"model": "chat-bard"},
                        "details": {"value": "100"},
                    },
                ],
            },
            {
                "name": "n2",
                "quotaId": "other",
                "quotaDisplayName": "Some unrelated quota",
                "metric": "m2",
                "dimensionsInfos": [
                    {
                        "dimensions": {"model": "gemini-2.5-flash"},
                        "details": {"value": "50"},
                    },
                ],
            },
        ]
        limiter = gqm.RateLimiter(rate=100)
        matches = gqm.find_matching_quotas(
            "my-proj",
            gqm.DEFAULT_SERVICE,
            _auth_headers(),
            gqm.DEFAULT_FILTERS,
            gqm.DEFAULT_MODELS,
            [],
            limiter,
            1,
        )
        assert len(matches) == 2
        assert all("gemini" in gqm.model_from_dimensions(m.dimensions) for m in matches)

    @patch.object(gqm, "list_quota_infos")
    def test_exclude_models(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            {
                "quotaId": "daily_rpd",
                "quotaDisplayName": ("Request limit per model per day for a project in the paid tier 3"),
                "metric": "m1",
                "dimensionsInfos": [
                    {
                        "dimensions": {"model": "gemini-1.5-flash-8b"},
                        "details": {"value": "100"},
                    },
                ],
            },
        ]
        limiter = gqm.RateLimiter(rate=100)
        matches = gqm.find_matching_quotas(
            "my-proj",
            gqm.DEFAULT_SERVICE,
            _auth_headers(),
            gqm.DEFAULT_FILTERS,
            gqm.DEFAULT_MODELS,
            ["flash-8b"],
            limiter,
            1,
        )
        assert matches == []


class TestGetAuthHeaders:
    @patch("gemini_quota_manager.google.auth.default")
    def test_sets_quota_project_header(self, mock_default: MagicMock) -> None:
        creds = MagicMock()
        creds.token = "test-token"
        creds.quota_project_id = "gemini-api-80295"
        creds.with_quota_project.return_value = creds
        mock_default.return_value = (creds, None)

        headers = gqm.get_auth_headers("gemini-api-80295")

        creds.with_quota_project.assert_called_once_with("gemini-api-80295")
        creds.refresh.assert_called_once()
        assert headers["Authorization"] == "Bearer test-token"
        assert headers["x-goog-user-project"] == "gemini-api-80295"


class TestValidateDecreaseAck:
    def test_ack_shorthand_passes(self) -> None:
        args = argparse.Namespace(ack_decrease_risks=True)
        gqm.validate_decrease_ack(args)  # no exit

    def test_both_flags_pass(self) -> None:
        args = argparse.Namespace(
            ack_decrease_risks=False,
            allow_high_percentage_quota_decrease=True,
            allow_quota_decrease_below_usage=True,
        )
        gqm.validate_decrease_ack(args)

    def test_missing_ack_exits(self) -> None:
        args = argparse.Namespace(
            ack_decrease_risks=False,
            allow_high_percentage_quota_decrease=False,
            allow_quota_decrease_below_usage=False,
        )
        with pytest.raises(SystemExit) as exc:
            gqm.validate_decrease_ack(args)
        assert exc.value.code == gqm.EXIT_ERROR


class TestParseArgs:
    def test_default_filters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["gemini_quota_manager.py", "--project", "p", "list"],
        )
        args = gqm.parse_args()
        assert args.filters == gqm.DEFAULT_FILTERS
        assert args.models == gqm.DEFAULT_MODELS
        assert args.service == gqm.DEFAULT_SERVICE
        assert args.project == "p"
        assert args.command == "list"


class TestProcessQuotaUpdate:
    @patch.object(gqm, "apply_update", return_value=True)
    def test_skips_when_reported_value_below_cap(self, _mock_apply: MagicMock) -> None:
        match = _quota_match(reported_value="500")
        args = argparse.Namespace(project="p", service=gqm.DEFAULT_SERVICE, skip_unknown_values=False, max_retries=1)
        limiter = gqm.RateLimiter(rate=10.0)
        outcome, ok = gqm.process_quota_update(match, 1000, args, _auth_headers(), limiter, dry_run=True, ignore_safety_checks=[], preference_lookup={})
        assert outcome == "skipped_below_cap"
        assert ok is False
        _mock_apply.assert_not_called()

    @patch.object(gqm, "apply_update", return_value=True)
    def test_caps_when_reported_value_above_cap(self, mock_apply: MagicMock) -> None:
        match = _quota_match(reported_value="5000")
        args = argparse.Namespace(project="p", service=gqm.DEFAULT_SERVICE, skip_unknown_values=False, max_retries=1)
        limiter = gqm.RateLimiter(rate=10.0)
        outcome, ok = gqm.process_quota_update(match, 1000, args, _auth_headers(), limiter, dry_run=True, ignore_safety_checks=[], preference_lookup={})
        assert outcome == "applied"
        assert ok is True
        mock_apply.assert_called_once()

    @patch.object(gqm, "apply_update", return_value=True)
    def test_disabled_model_with_unknown_value_shows_disable_not_unknown(self, mock_apply: MagicMock) -> None:
        match = _quota_match(reported_value="n/a", dimensions={"model": "gemini-2.5-flash-native-audio-dialog"})
        args = argparse.Namespace(project="p", service=gqm.DEFAULT_SERVICE, skip_unknown_values=False, max_retries=1)
        limiter = gqm.RateLimiter(rate=10.0)
        outcome, ok = gqm.process_quota_update(match, 0, args, _auth_headers(), limiter, dry_run=True, ignore_safety_checks=[], preference_lookup={})
        assert outcome == "applied"
        assert ok is True
        mock_apply.assert_called_once()


class TestCmdUpdateInterrupt:
    @patch.object(gqm, "list_quota_preferences", return_value=[])
    @patch.object(gqm, "process_quota_update")
    @patch.object(gqm, "find_matching_quotas")
    @patch.object(gqm, "get_auth_headers")
    def test_ctrl_c_prints_summary_and_exits(
        self,
        _mock_auth: MagicMock,
        mock_find: MagicMock,
        mock_process: MagicMock,
        _mock_prefs: MagicMock,
    ) -> None:
        mock_find.return_value = [_quota_match(), _quota_match(dimensions={"model": "gemini-2.5-pro"})]
        mock_process.side_effect = [("applied", True), KeyboardInterrupt()]

        args = argparse.Namespace(
            project="my-proj",
            service=gqm.DEFAULT_SERVICE,
            filters=gqm.DEFAULT_FILTERS,
            models=gqm.DEFAULT_MODELS,
            exclude_models=[],
            value=1000,
            apply=False,
            ack_decrease_risks=True,
            max_retries=1,
            rps=10.0,
            burst=1,
        )

        with pytest.raises(SystemExit) as exc:
            gqm.cmd_update(args)
        assert exc.value.code == gqm.EXIT_INTERRUPTED
        assert mock_process.call_count == 2


class TestRequestWithBackoff:
    @patch.object(gqm.time, "sleep")
    @patch.object(gqm.requests, "request")
    def test_retries_on_429_then_succeeds(self, mock_request: MagicMock, _mock_sleep: MagicMock) -> None:
        rate_limited = MagicMock(status_code=429, headers={})
        ok = MagicMock(status_code=200, text="{}")
        mock_request.side_effect = [rate_limited, ok]
        limiter = gqm.RateLimiter(rate=100.0)
        resp = gqm.request_with_backoff(limiter, "GET", "https://example.com", max_retries=2)
        assert resp.status_code == 200
        assert mock_request.call_count == 2


class TestListQuotaInfos:
    @patch.object(gqm, "request_with_backoff")
    def test_paginates_quota_infos(self, mock_request: MagicMock) -> None:
        mock_request.side_effect = [
            MagicMock(status_code=200, json=lambda: {"quotaInfos": [{"quotaId": "q1"}], "nextPageToken": "t2"}),
            MagicMock(status_code=200, json=lambda: {"quotaInfos": [{"quotaId": "q2"}]}),
        ]
        limiter = gqm.RateLimiter(rate=100.0)
        infos = gqm.list_quota_infos("p", gqm.DEFAULT_SERVICE, _auth_headers(), limiter, 1)
        assert len(infos) == 2
        assert mock_request.call_count == 2

    @patch.object(gqm, "request_with_backoff")
    def test_api_error_exits(self, mock_request: MagicMock) -> None:
        mock_request.return_value = MagicMock(status_code=403, text="denied")
        limiter = gqm.RateLimiter(rate=100.0)
        with pytest.raises(SystemExit) as exc:
            gqm.list_quota_infos("p", gqm.DEFAULT_SERVICE, _auth_headers(), limiter, 1)
        assert exc.value.code == gqm.EXIT_ERROR


class TestListQuotaPreferences:
    @patch.object(gqm, "request_with_backoff")
    def test_lists_preferences(self, mock_request: MagicMock) -> None:
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: {"quotaPreferences": [{"name": "projects/p/locations/global/quotaPreferences/x"}]},
        )
        limiter = gqm.RateLimiter(rate=100.0)
        prefs = gqm.list_quota_preferences("p", _auth_headers(), limiter, 1)
        assert len(prefs) == 1


class TestBuildPreferenceLookup:
    def test_skips_entries_without_name(self) -> None:
        lookup = gqm.build_preference_lookup([{"service": gqm.DEFAULT_SERVICE, "quotaId": "q", "dimensions": {}}])
        assert lookup == {}


class TestGeminiModelTierUnknown:
    def test_unknown_tier_suffix(self) -> None:
        assert gqm.gemini_model_tier("gemini-2.5-thinking") == (2, 5, gqm.TIER_UNKNOWN)


class TestFormatReportedValueEdge:
    def test_none_reported_value(self) -> None:
        assert gqm.format_reported_value(_quota_match(reported_value=None)) == "-"


class TestApplyUpdateFailure:
    @patch.object(gqm, "request_with_backoff")
    def test_returns_false_on_error(self, mock_request: MagicMock) -> None:
        mock_request.return_value = MagicMock(status_code=400, text="bad")
        match = _quota_match()
        limiter = gqm.RateLimiter(rate=100.0)
        ok = gqm.apply_update("my-proj", gqm.DEFAULT_SERVICE, _auth_headers(), match, 100, False, limiter, 1)
        assert ok is False


class TestCmdList:
    @patch.object(gqm, "find_matching_quotas")
    @patch.object(gqm, "get_auth_headers")
    def test_cmd_list_prints_matches(self, _mock_auth: MagicMock, mock_find: MagicMock) -> None:
        mock_find.return_value = [_quota_match()]
        args = argparse.Namespace(
            project="p",
            service=gqm.DEFAULT_SERVICE,
            filters=gqm.DEFAULT_FILTERS,
            models=gqm.DEFAULT_MODELS,
            exclude_models=[],
            max_retries=1,
            rps=10.0,
            burst=1,
            value=gqm.DEFAULT_CAP_VALUE,
        )
        gqm.cmd_list(args)
        mock_find.assert_called_once()


class TestProcessQuotaUpdateOutcomes:
    @patch.object(gqm, "apply_update", return_value=True)
    def test_unlimited_quota_proceeds(self, mock_apply: MagicMock) -> None:
        match = _quota_match(reported_value="-1")
        args = argparse.Namespace(project="p", service=gqm.DEFAULT_SERVICE, skip_unknown_values=False, max_retries=1)
        limiter = gqm.RateLimiter(rate=10.0)
        outcome, ok = gqm.process_quota_update(match, 1000, args, _auth_headers(), limiter, True, [], {})
        assert outcome == "applied"
        assert ok is True
        mock_apply.assert_called_once()

    def test_skipped_at_target(self) -> None:
        match = _quota_match(reported_value="1000")
        args = argparse.Namespace(project="p", service=gqm.DEFAULT_SERVICE, skip_unknown_values=False, max_retries=1)
        limiter = gqm.RateLimiter(rate=10.0)
        outcome, ok = gqm.process_quota_update(match, 1000, args, _auth_headers(), limiter, True, [], {})
        assert outcome == "skipped_at_target"
        assert ok is False

    @patch.object(gqm, "apply_update", return_value=True)
    def test_skipped_unknown_when_flag_set(self, _mock_apply: MagicMock) -> None:
        match = _quota_match(reported_value="n/a")
        args = argparse.Namespace(project="p", service=gqm.DEFAULT_SERVICE, skip_unknown_values=True, max_retries=1)
        limiter = gqm.RateLimiter(rate=10.0)
        outcome, ok = gqm.process_quota_update(match, 1000, args, _auth_headers(), limiter, True, [], {})
        assert outcome == "skipped_unknown"
        assert ok is False


class TestCmdUpdateOutcomes:
    @patch.object(gqm, "list_quota_preferences", return_value=[])
    @patch.object(gqm, "find_matching_quotas", return_value=[])
    @patch.object(gqm, "get_auth_headers")
    def test_no_matches_returns_early(self, _mock_auth: MagicMock, _mock_find: MagicMock, _mock_prefs: MagicMock) -> None:
        args = argparse.Namespace(
            project="p",
            service=gqm.DEFAULT_SERVICE,
            filters=gqm.DEFAULT_FILTERS,
            models=gqm.DEFAULT_MODELS,
            exclude_models=[],
            value=1000,
            apply=False,
            ack_decrease_risks=True,
            max_retries=1,
            rps=10.0,
            burst=1,
        )
        gqm.cmd_update(args)

    @patch.object(gqm, "list_quota_preferences", return_value=[])
    @patch.object(gqm, "process_quota_update")
    @patch.object(gqm, "find_matching_quotas")
    @patch.object(gqm, "get_auth_headers")
    def test_partial_failure_exits_two(
        self,
        _mock_auth: MagicMock,
        mock_find: MagicMock,
        mock_process: MagicMock,
        _mock_prefs: MagicMock,
    ) -> None:
        mock_find.return_value = [_quota_match()]
        mock_process.return_value = ("applied", False)
        args = argparse.Namespace(
            project="p",
            service=gqm.DEFAULT_SERVICE,
            filters=gqm.DEFAULT_FILTERS,
            models=gqm.DEFAULT_MODELS,
            exclude_models=[],
            value=1000,
            apply=True,
            ack_decrease_risks=True,
            max_retries=1,
            rps=10.0,
            burst=1,
        )
        with pytest.raises(SystemExit) as exc:
            gqm.cmd_update(args)
        assert exc.value.code == gqm.EXIT_PARTIAL

    @patch.object(gqm, "list_quota_preferences", return_value=[])
    @patch.object(gqm, "process_quota_update")
    @patch.object(gqm, "find_matching_quotas")
    @patch.object(gqm, "get_auth_headers")
    def test_counts_skip_outcomes(
        self,
        _mock_auth: MagicMock,
        mock_find: MagicMock,
        mock_process: MagicMock,
        _mock_prefs: MagicMock,
    ) -> None:
        mock_find.return_value = [_quota_match(), _quota_match(dimensions={"model": "gemini-2.5-pro"})]
        mock_process.side_effect = [
            ("skipped_at_target", False),
            ("skipped_unknown", False),
        ]
        args = argparse.Namespace(
            project="p",
            service=gqm.DEFAULT_SERVICE,
            filters=gqm.DEFAULT_FILTERS,
            models=gqm.DEFAULT_MODELS,
            exclude_models=[],
            value=1000,
            apply=False,
            ack_decrease_risks=True,
            max_retries=1,
            rps=10.0,
            burst=1,
        )
        gqm.cmd_update(args)
        assert mock_process.call_count == 2


class TestMain:
    @patch.object(gqm, "parse_args")
    def test_keyboard_interrupt_exits_130(self, mock_parse: MagicMock) -> None:
        mock_parse.side_effect = KeyboardInterrupt()
        with pytest.raises(SystemExit) as exc:
            gqm.main()
        assert exc.value.code == gqm.EXIT_INTERRUPTED
