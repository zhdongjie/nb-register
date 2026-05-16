import unittest
from unittest.mock import patch

import sms_pb2
import herosms_sms_server


class ActiveContext:
    def is_active(self):
        return True


def provider_config():
    return sms_pb2.SmsProviderConfig(
        config_id="default",
        provider="herosms",
        enabled=True,
        api_base="https://hero-sms.example/api",
        api_key="secret",
        default_service="ni",
        default_country=6,
        default_country_calling_code="62",
        default_max_price=0.05,
    )


class FakeStore:
    def __init__(self):
        self.configs = {}

    def upsert(self, config):
        config = herosms_sms_server._config_from_pb(config)
        self.configs[config.config_id] = config
        return config

    def get(self, config_id):
        return self.configs.get(herosms_sms_server._normalize_config_id(config_id))

    def list(self, include_disabled):
        values = list(self.configs.values())
        if include_disabled:
            return values
        return [config for config in values if config.enabled]

    def delete(self, config_id):
        return self.configs.pop(herosms_sms_server._normalize_config_id(config_id), None) is not None

    def _bootstrap_from_env(self):
        return None


class HeroSMSSmsTests(unittest.TestCase):
    def test_provider_crud_uses_store(self):
        store = FakeStore()
        with patch.object(herosms_sms_server, "_config_store", return_value=store):
            servicer = herosms_sms_server.SmsServicer()
            upsert = servicer.UpsertProvider(
                sms_pb2.UpsertSmsProviderRequest(config=provider_config()),
                None,
            )
            listed = servicer.ListProviders(sms_pb2.ListSmsProvidersRequest(), None)
            deleted = servicer.DeleteProvider(sms_pb2.DeleteSmsProviderRequest(config_id="default"), None)

        self.assertTrue(upsert.success)
        self.assertTrue(listed.success)
        self.assertEqual([config.config_id for config in listed.configs], ["default"])
        self.assertTrue(deleted.success)

    def test_acquire_number_uses_provider_config(self):
        config = provider_config()
        with patch.object(herosms_sms_server, "_load_provider_config", return_value=config), \
             patch.object(herosms_sms_server, "_provider_call", return_value="ACCESS_NUMBER:new-id:6281299999999") as call:
            resp = herosms_sms_server.SmsServicer().AcquireNumber(
                sms_pb2.AcquireNumberRequest(),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(resp.activation_id, "new-id")
        self.assertEqual(resp.phone, "81299999999")
        self.assertEqual(resp.provider, "herosms")
        call.assert_called_once_with(config, "getNumber", service="ni", country=6, maxPrice=0.05)

    def test_wait_code_polls_status_by_activation_id(self):
        config = provider_config()
        with patch.object(herosms_sms_server, "_load_provider_config", return_value=config), \
             patch.object(herosms_sms_server, "_provider_call", return_value="STATUS_OK:123456") as call:
            resp = herosms_sms_server.SmsServicer().WaitCode(
                sms_pb2.WaitCodeRequest(activation_id="act-1", timeout_seconds=1),
                ActiveContext(),
            )

        self.assertTrue(resp.success)
        self.assertEqual(resp.code, "123456")
        call.assert_called_once_with(config, "getStatus", id="act-1")

    def test_cancel_activation_proxies_status(self):
        config = provider_config()
        with patch.object(herosms_sms_server, "_load_provider_config", return_value=config), \
             patch.object(herosms_sms_server, "_provider_call", return_value="ACCESS_CANCEL") as call:
            resp = herosms_sms_server.SmsServicer().CancelActivation(
                sms_pb2.CancelActivationRequest(activation_id="act-1"),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(resp.raw_response, "ACCESS_CANCEL")
        call.assert_called_once_with(config, "setStatus", id="act-1", status=8)

    def test_finish_activation_proxies_status(self):
        config = provider_config()
        with patch.object(herosms_sms_server, "_load_provider_config", return_value=config), \
             patch.object(herosms_sms_server, "_provider_call", return_value="ACCESS_ACTIVATION") as call:
            resp = herosms_sms_server.SmsServicer().FinishActivation(
                sms_pb2.FinishActivationRequest(activation_id="act-1"),
                None,
            )

        self.assertTrue(resp.success)
        self.assertEqual(resp.raw_response, "ACCESS_ACTIVATION")
        call.assert_called_once_with(config, "setStatus", id="act-1", status=6)


if __name__ == "__main__":
    unittest.main()
