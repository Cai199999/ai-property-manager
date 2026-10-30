package com.example.propertyassistant.client;

import com.example.propertyassistant.config.GatewayProperties;
import com.example.propertyassistant.dto.AssistantQueryRequest;
import com.example.propertyassistant.dto.CodeQueryRequest;
import com.example.propertyassistant.dto.DeleteAllConfirmationRequest;
import com.example.propertyassistant.exception.InvalidUploadException;
import com.example.propertyassistant.exception.UploadTooLargeException;
import com.example.propertyassistant.exception.UpstreamAgentException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.ContentDisposition;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;
import org.springframework.web.multipart.MultipartFile;

import java.io.FilterInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 调用 Python Agent 的 JSON 接口。
 *
 * 网关不承载业务字段映射：成功响应一律解析为 JsonNode 后原样返回，Python
 * 新增 chart_meta、download_url 等字段不需要修改 Java DTO。
 */
@Service
public class AgentGatewayService {

    private static final Logger log = LoggerFactory.getLogger(AgentGatewayService.class);
    private static final int MAX_ERROR_BYTES = 8 * 1024;
    private static final MediaType XLSX_MEDIA_TYPE = MediaType.parseMediaType(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    );
    private final RestClient restClient;
    private final GatewayProperties properties;
    private final ObjectMapper objectMapper;

    public AgentGatewayService(
            @Qualifier("agentRestClient") RestClient restClient,
            GatewayProperties properties,
            ObjectMapper objectMapper
    ) {
        this.restClient = restClient;
        this.properties = properties;
        this.objectMapper = objectMapper;
    }

    public JsonNode query(AssistantQueryRequest request, String requestId) {
        return postJson(properties.queryPath(), request, requestId);
    }

    public JsonNode codeQuery(CodeQueryRequest request, String requestId) {
        return postJson(properties.codeQueryPath(), request, requestId);
    }

    public JsonNode confirmDeleteAll(
            DeleteAllConfirmationRequest request,
            String requestId
    ) {
        return postJson(properties.deleteAllConfirmPath(), request, requestId);
    }

    public JsonNode listTables(String requestId) {
        try {
            return restClient.get()
                    .uri(properties.tablesPath())
                    .header("X-Request-Id", requestId)
                    .accept(MediaType.APPLICATION_JSON)
                    .exchange((clientRequest, clientResponse) ->
                            readJsonResponse(clientResponse, requestId));
        } catch (UpstreamAgentException exception) {
            log.warn("Agent 返回异常 requestId={} upstreamStatus={}",
                    requestId, exception.getUpstreamStatus());
            throw exception;
        } catch (ResourceAccessException exception) {
            log.error("无法连接 Python Agent requestId={}", requestId, exception);
            throw new UpstreamAgentException(503, "Python Agent 服务不可达", requestId);
        } catch (RestClientException exception) {
            log.error("Agent 调用失败 requestId={}", requestId, exception);
            throw new UpstreamAgentException(502, "Python Agent 调用失败", requestId);
        }
    }

    public JsonNode previewTable(String name, int offset, int limit, String requestId) {
        try {
            return restClient.get()
                    .uri(uriBuilder -> uriBuilder
                            .path(properties.previewPath())
                            .queryParam("name", name)
                            .queryParam("offset", offset)
                            .queryParam("limit", limit)
                            .build())
                    .header("X-Request-Id", requestId)
                    .accept(MediaType.APPLICATION_JSON)
                    .exchange((clientRequest, clientResponse) ->
                            readJsonResponse(clientResponse, requestId));
        } catch (UpstreamAgentException exception) {
            log.warn("Agent 预览返回异常 requestId={} upstreamStatus={}",
                    requestId, exception.getUpstreamStatus());
            throw exception;
        } catch (ResourceAccessException exception) {
            log.error("无法连接 Python Agent requestId={}", requestId, exception);
            throw new UpstreamAgentException(503, "Python Agent 服务不可达", requestId);
        } catch (RestClientException exception) {
            log.error("Agent 预览失败 requestId={}", requestId, exception);
            throw new UpstreamAgentException(502, "Python Agent 调用失败", requestId);
        }
    }

    public JsonNode uploadTables(List<MultipartFile> files, String requestId) {
        if (files == null || files.isEmpty()) {
            throw new InvalidUploadException("请选择至少一个 .xlsx 文件");
        }
        if (files.size() > properties.uploadMaxFiles()) {
            throw new InvalidUploadException(
                    "单次最多上传 " + properties.uploadMaxFiles() + " 个文件"
            );
        }

        List<String> filenames = new ArrayList<>(files.size());
        long totalBytes = 0;
        for (MultipartFile file : files) {
            filenames.add(validateUpload(file));
            totalBytes += file.getSize();
        }
        if (totalBytes > properties.uploadMaxBytes()) {
            throw new UploadTooLargeException(
                    "上传文件总大小超过 "
                            + properties.uploadMaxBytes() / 1_000_000
                            + " MB 限制"
            );
        }

        // 手动组装 multipart，避免 MultipartBodyBuilder 引入 Reactor 的
        // Publisher 类型，保持网关只依赖 Spring MVC + Jackson。
        // 文件段只带 ASCII 文件名：ContentDisposition 带 charset 时会写成
        // RFC 2047 encoded-word，Python 解析后无法匹配 .xlsx 后缀。
        // 原始中文名改为独立 filename 字段，按上传顺序重复传递。
        HttpHeaders partHeaders = new HttpHeaders();
        partHeaders.setContentType(XLSX_MEDIA_TYPE);
        HttpHeaders filenameHeaders = new HttpHeaders();
        filenameHeaders.setContentDisposition(
                ContentDisposition.formData().name("filename").build()
        );

        MultiValueMap<String, Object> parts = new LinkedMultiValueMap<>();
        for (int index = 0; index < files.size(); index++) {
            String safeName = "upload_" + (index + 1) + ".xlsx";
            HttpHeaders headers = new HttpHeaders();
            headers.putAll(partHeaders);
            headers.setContentDisposition(
                    ContentDisposition.formData()
                            .name("file")
                            .filename(safeName)
                            .build()
            );
            parts.add("file", new HttpEntity<>(files.get(index).getResource(), headers));
        }
        for (String filename : filenames) {
            parts.add("filename", new HttpEntity<>(filename, filenameHeaders));
        }

        try {
            return restClient.post()
                    .uri(properties.uploadPath())
                    .header("X-Request-Id", requestId)
                    .contentType(MediaType.MULTIPART_FORM_DATA)
                    .accept(MediaType.APPLICATION_JSON)
                    .body(parts)
                    .exchange((clientRequest, clientResponse) ->
                            readJsonResponse(clientResponse, requestId));
        } catch (UpstreamAgentException exception) {
            log.warn("Agent 上传返回异常 requestId={} upstreamStatus={}",
                    requestId, exception.getUpstreamStatus());
            throw exception;
        } catch (ResourceAccessException exception) {
            log.error("无法连接 Python Agent requestId={}", requestId, exception);
            throw new UpstreamAgentException(503, "Python Agent 服务不可达", requestId);
        } catch (RestClientException exception) {
            log.error("Agent 上传失败 requestId={}", requestId, exception);
            throw new UpstreamAgentException(502, "Python Agent 调用失败", requestId);
        }
    }

    private JsonNode postJson(String path, Object body, String requestId) {
        try {
            return restClient.post()
                    .uri(path)
                    .header("X-Request-Id", requestId)
                    .contentType(MediaType.APPLICATION_JSON)
                    .accept(MediaType.APPLICATION_JSON)
                    .body(body)
                    .exchange((clientRequest, clientResponse) ->
                            readJsonResponse(clientResponse, requestId));
        } catch (UpstreamAgentException exception) {
            log.warn("Agent 返回异常 requestId={} upstreamStatus={}",
                    requestId, exception.getUpstreamStatus());
            throw exception;
        } catch (ResourceAccessException exception) {
            log.error("无法连接 Python Agent requestId={}", requestId, exception);
            throw new UpstreamAgentException(503, "Python Agent 服务不可达", requestId);
        } catch (RestClientException exception) {
            log.error("Agent 调用失败 requestId={}", requestId, exception);
            throw new UpstreamAgentException(502, "Python Agent 调用失败", requestId);
        }
    }

    private JsonNode readJsonResponse(
            org.springframework.http.client.ClientHttpResponse response,
            String requestId
    ) throws IOException {
        HttpStatusCode status = response.getStatusCode();
        if (!status.is2xxSuccessful()) {
            byte[] errorBody = readBounded(response.getBody(), MAX_ERROR_BYTES);
            throw new UpstreamAgentException(
                    status.value(),
                    extractUpstreamMessage(errorBody, status.value()),
                    requestId
            );
        }

        try (InputStream body = response.getBody()) {
            return objectMapper.readTree(
                    new LimitedInputStream(body, properties.maxJsonResponseBytes())
            );
        } catch (IOException exception) {
            log.error("解析 Agent JSON 响应失败 requestId={}", requestId, exception);
            throw new UpstreamAgentException(502, "Python Agent 返回格式异常", requestId);
        }
    }

    private String validateUpload(MultipartFile file) {
        if (file == null || file.isEmpty()) {
            throw new InvalidUploadException("上传文件为空");
        }
        if (file.getSize() > properties.uploadMaxBytes()) {
            throw new UploadTooLargeException(
                    "文件超过 " + properties.uploadMaxBytes() / 1_000_000 + " MB 限制"
            );
        }

        String original = file.getOriginalFilename();
        String normalized = original == null ? "" : original.replace('\\', '/');
        String filename = normalized.substring(normalized.lastIndexOf('/') + 1).trim();
        if (filename.isBlank()
                || filename.startsWith("~$")
                || !filename.toLowerCase(Locale.ROOT).endsWith(".xlsx")) {
            throw new InvalidUploadException("只支持 .xlsx 文件");
        }
        return filename;
    }

    private byte[] readBounded(InputStream input, int maxBytes) {
        try (InputStream body = input) {
            return body.readNBytes(maxBytes);
        } catch (IOException exception) {
            return new byte[0];
        }
    }

    private String extractUpstreamMessage(byte[] body, int status) {
        try {
            JsonNode root = objectMapper.readTree(body);
            JsonNode message = root.path("error").path("message");
            if (!message.isMissingNode() && !message.asText().isBlank()) {
                return message.asText();
            }
        } catch (IOException ignored) {
            // 非 JSON 错误体直接使用通用提示，避免把上游 HTML 泄漏给调用方。
        }
        return "Python Agent 调用失败，HTTP " + status;
    }

    /**
     * 限制 JSON 响应读取量，防止异常上游返回超大响应撑爆堆内存。
     */
    private static final class LimitedInputStream extends FilterInputStream {

        private final long limit;
        private long consumed;

        private LimitedInputStream(InputStream input, long limit) {
            super(input);
            this.limit = limit;
        }

        @Override
        public int read() throws IOException {
            int value = super.read();
            if (value >= 0) {
                consume(1);
            }
            return value;
        }

        @Override
        public int read(byte[] buffer, int offset, int length) throws IOException {
            int count = super.read(buffer, offset, length);
            if (count > 0) {
                consume(count);
            }
            return count;
        }

        private void consume(int count) throws IOException {
            consumed += count;
            if (consumed > limit) {
                throw new IOException("上游响应超过大小限制");
            }
        }
    }
}
