"""MinerU HTTP contract and failures, without external services or model deps."""
import httpx
import pytest

from backend.attachments import DocumentExtractionError, MinerUConfig, MinerUExtractor


@pytest.mark.asyncio
@pytest.mark.parametrize('name,mime', [('scan.pdf', 'application/pdf'), ('scan.PNG', 'image/png')])
async def test_extract_document_over_json_api(name, mime):
    def respond(request):
        assert str(request.url) == 'http://mineru.test/file_parse'
        body = request.read().decode()
        assert 'vlm-engine' in body and mime in body and f'filename="{name}"' in body
        assert 'authorization' not in request.headers
        return httpx.Response(200, json={'results': {'scan': {'md_content': '# 制度\n报销金额 1250 元'}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await MinerUExtractor(MinerUConfig('http://mineru.test/'), client=client).extract(b'scan', name)
        assert result.markdown == '# 制度\n报销金额 1250 元'
        assert result.file_name == name and result.provider == 'mineru'
        assert not client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [None, [], {}, {'results': {}}, {'results': {'scan': {}}}, {'results': {'scan': {'md_content': ''}}}])
async def test_missing_or_empty_text_is_a_failure(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(DocumentExtractionError):
            await MinerUExtractor(MinerUConfig('http://mineru.test'), client=client).extract(b'scan', 'scan.pdf')


@pytest.mark.asyncio
async def test_http_error_and_optional_token():
    def respond(request):
        assert request.headers['authorization'] == 'Bearer test-token'
        return httpx.Response(503, json={'detail': 'model unavailable'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DocumentExtractionError, match='model unavailable'):
            await MinerUExtractor(MinerUConfig('http://mineru.test', token='test-token'), client=client).extract(b'scan', 'scan.pdf')


@pytest.mark.asyncio
async def test_timeout_propagates_as_extraction_failure():
    def respond(request):
        raise httpx.ReadTimeout('timeout', request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DocumentExtractionError, match='ReadTimeout'):
            await MinerUExtractor(MinerUConfig('http://mineru.test'), client=client).extract(b'scan', 'scan.pdf')


@pytest.mark.asyncio
@pytest.mark.parametrize('content,name', [(b'', 'scan.pdf'), (b'office', 'file.docx')])
async def test_invalid_input_does_not_call_service(content, name):
    def respond(request):
        pytest.fail('invalid input must not call MinerU')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DocumentExtractionError):
            await MinerUExtractor(MinerUConfig('http://mineru.test'), client=client).extract(content, name)
