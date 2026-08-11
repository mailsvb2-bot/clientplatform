from __future__ import annotations

import base64
import unittest
from unittest.mock import Mock

from clientplatform.integrations.yandex_direct import YandexOAuthConfig
from clientplatform.integrations.yandex_direct_media import MediaAwareYandexDirectProvider


class YandexDirectMediaTests(unittest.TestCase):
    def provider(self) -> MediaAwareYandexDirectProvider:
        return MediaAwareYandexDirectProvider(
            oauth=YandexOAuthConfig(
                client_id="client-id",
                redirect_uri="https://clientplatform.example/oauth/callback",
            )
        )

    def test_copy_sync_updates_exact_provider_draft(self) -> None:
        provider = self.provider()
        direct = Mock(return_value={"UpdateResults": [{"Id": 501}]})
        provider._direct_call = direct  # type: ignore[method-assign]
        provider.update_copy(
            access_token="token",
            ad_id="501",
            title="  Мой   заголовок ",
            text="  Мой   текст ",
            href="https://example.test/go",
        )
        update = direct.call_args.kwargs
        self.assertEqual(update["service"], "ads")
        ad = update["payload"]["params"]["Ads"][0]
        self.assertEqual(ad["Id"], 501)
        self.assertEqual(ad["TextAd"]["Title"], "Мой заголовок")
        self.assertEqual(ad["TextAd"]["Text"], "Мой текст")
        self.assertEqual(ad["TextAd"]["Href"], "https://example.test/go")

    def test_image_upload_and_attachment_use_native_direct_fields(self) -> None:
        provider = self.provider()
        direct = Mock(
            side_effect=[
                {"AddResults": [{"AdImageHash": "hash-1"}]},
                {"UpdateResults": [{"Id": 501}]},
            ]
        )
        provider._direct_call = direct  # type: ignore[method-assign]
        image_hash = provider.upload_image(
            access_token="token",
            payload=b"jpeg-bytes",
            name="my.jpg",
        )
        provider.attach_image(
            access_token="token",
            ad_id="501",
            image_hash=image_hash,
        )
        self.assertEqual(image_hash, "hash-1")
        upload = direct.call_args_list[0].kwargs
        self.assertEqual(upload["service"], "adimages")
        image = upload["payload"]["params"]["AdImages"][0]
        self.assertEqual(image["ImageData"], base64.b64encode(b"jpeg-bytes").decode("ascii"))
        self.assertEqual(image["Type"], "AUTO")
        update = direct.call_args_list[1].kwargs
        self.assertEqual(update["service"], "ads")
        ad = update["payload"]["params"]["Ads"][0]
        self.assertEqual(ad["TextAd"]["AdImageHash"], "hash-1")
        self.assertIsNone(ad["TextAd"]["VideoExtension"]["CreativeId"])

    def test_clear_media_removes_old_image_and_video_extension(self) -> None:
        provider = self.provider()
        direct = Mock(return_value={"UpdateResults": [{"Id": 501}]})
        provider._direct_call = direct  # type: ignore[method-assign]
        provider.clear_media(access_token="token", ad_id="501")
        ad = direct.call_args.kwargs["payload"]["params"]["Ads"][0]
        self.assertIsNone(ad["TextAd"]["AdImageHash"])
        self.assertIsNone(ad["TextAd"]["VideoExtension"]["CreativeId"])

    def test_video_upload_conversion_creative_and_attachment_use_native_direct_fields(self) -> None:
        provider = self.provider()
        direct = Mock(
            side_effect=[
                {"AddResults": [{"Id": "video-1"}]},
                {"AdVideos": [{"Id": "video-1", "Status": "READY"}]},
                {"AddResults": [{"Id": 777}]},
                {"UpdateResults": [{"Id": 501}]},
            ]
        )
        provider._direct_call = direct  # type: ignore[method-assign]
        video_id = provider.upload_video(
            access_token="token",
            payload=b"video-bytes",
            name="mine.mp4",
        )
        self.assertEqual(provider.video_status(access_token="token", video_id=video_id), "READY")
        creative_id = provider.create_video_extension(
            access_token="token",
            video_id=video_id,
        )
        provider.attach_video(
            access_token="token",
            ad_id="501",
            creative_id=creative_id,
        )
        self.assertEqual(video_id, "video-1")
        self.assertEqual(creative_id, "777")
        upload = direct.call_args_list[0].kwargs["payload"]["params"]["AdVideos"][0]
        self.assertEqual(upload["VideoData"], base64.b64encode(b"video-bytes").decode("ascii"))
        status_payload = direct.call_args_list[1].kwargs["payload"]
        self.assertEqual(status_payload["params"]["SelectionCriteria"]["Ids"], ["video-1"])
        creative = direct.call_args_list[2].kwargs["payload"]["params"]["Creatives"][0]
        self.assertEqual(creative["VideoExtensionCreative"]["VideoId"], "video-1")
        ad = direct.call_args_list[3].kwargs["payload"]["params"]["Ads"][0]
        self.assertEqual(ad["TextAd"]["VideoExtension"]["CreativeId"], 777)
        self.assertIsNone(ad["TextAd"]["AdImageHash"])

    def test_video_pending_status_is_preserved(self) -> None:
        provider = self.provider()
        provider._direct_call = Mock(  # type: ignore[method-assign]
            return_value={"AdVideos": [{"Id": "video-1", "Status": "CONVERTING"}]}
        )
        self.assertEqual(
            provider.video_status(access_token="token", video_id="video-1"),
            "CONVERTING",
        )


if __name__ == "__main__":
    unittest.main()
