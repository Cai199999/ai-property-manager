package com.example.propertyassistant.client;

import com.example.propertyassistant.config.GatewayProperties;
import com.example.propertyassistant.exception.DownloadBusyException;
import com.example.propertyassistant.exception.DownloadTooLargeException;
import com.example.propertyassistant.exception.InvalidDownloadNameException;
import com.example.propertyassistant.exception.UpstreamAgentException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.ContentDisposition;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.Locale;
import java.util.concurrent.Semaphore;
import java.util.regex.Pattern;

/**
 * 查询结果文件下载代理（Excel / Word）。
 *
 * 全程流式复制，不把文件读成 byte[]；并发数由 Semaphore 限制，避免大量
 * 下载同时占用堆内存和上游连接。
 */
@Service
public class AgentDownloadService {

    private static final Logger log = LoggerFactory.getLogger(AgentDownloadService.class);
    private static final int BUFFER_SIZE = 64 * 1024;
    private static final int MAX_ERROR_BYTES = 8 * 1024;
    private static final Pattern SAFE_FILENAME = Pattern.compile(
            "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\\.(?:xlsx|docx)$"
    );
    private static final MediaType XLSX_MEDIA_TYPE = MediaType.parseMediaType(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    );
    private static final MediaType DOCX_MEDIA_TYPE = MediaType.parseMediaType(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    );

    private final RestClient restClient;
    private final GatewayProperties properties;
    private final ObjectMapper objectMapper;
    private final Semaphore slots;

    public AgentDownloadService(
            @Qualifier("agentDownloadRestClient") RestClient restClient,
            GatewayProperties properties,
            ObjectMapper objectMapper
    ) {
        this.restClient = restClient;
        this.properties = properties;
        this.objectMapper = objectMapper;
        this.slots = new Semaphore(properties.downloadMaxConcurrent());
    }

    public void stream(String filename, String requestId, HttpServletResponse response) {
        validateFilename(filename);
        if (!slots.tryAcquire()) {
            throw new DownloadBusyException();
        }

        try {
            restClient.get()
                    .uri(properties.downloadPath() + "/{filename}", filename)
                    .header("X-Request-Id", requestId)
                    .accept(
                            MediaType.APPLICATION_OCTET_STREAM,
                            XLSX_MEDIA_TYPE,
                            DOCX_MEDIA_TYPE
                    )
                    .exchange((clientRequest, clientResponse) -> {
                        copyResponse(clientResponse, filename, requestId, response);
                        return null;
                    });
        } catch (UpstreamAgentException | DownloadTooLargeException exception) {
            if (response.isCommitted()) {
                log.warn("下载响应已提交，无法再返回错误 requestId={} filename={}",
                        requestId, filename);
                return;
            }
            throw exception;
        } catch (ResourceAccessException exception) {
            log.error("无法连接 Python Agent 下载 requestId={}", requestId, exception);
            throw new UpstreamAgentException(503, "Python Agent 服务不可达", requestId);
        } catch (RestClientException exception) {
            log.error("Agent 下载失败 requestId={}", requestId, exception);
            throw new UpstreamAgentException(502, "Python Agent 调用失败", requestId);
        } finally {
            slots.release();
        }
    }

    private void copyResponse(
            org.springframework.http.client.ClientHttpResponse clientResponse,
            String filename,
            String requestId,
            HttpServletResponse response
    ) throws IOException {
        HttpStatusCode status = clientResponse.getStatusCode();
        if (!status.is2xxSuccessful()) {
            byte[] errorBody = readBounded(clientResponse.getBody(), MAX_ERROR_BYTES);
            throw new UpstreamAgentException(
                    status.value(),
                    extractUpstreamMessage(errorBody, status.value()),
                    requestId
            );
        }

        long contentLength = clientResponse.getHeaders().getContentLength();
        if (contentLength > properties.downloadMaxBytes()) {
            throw new DownloadTooLargeException("下载文件超过大小限制");
        }

        response.setStatus(HttpStatus.OK.value());
        // 扩展名已在入口处通过白名单校验，这里只按后缀选择 MIME。
        response.setContentType(mediaTypeFor(filename).toString());
        // 文件名已由白名单限制为纯 ASCII，不需要 RFC 2047 编码；
        // 带 charset 会生成 =?UTF-8?Q?...?= 的 encoded-word，浏览器兼容性更差。
        response.setHeader(
                HttpHeaders.CONTENT_DISPOSITION,
                ContentDisposition.attachment()
                        .filename(filename)
                        .build()
                        .toString()
        );
        response.setHeader("X-Content-Type-Options", "nosniff");
        response.setHeader(HttpHeaders.CACHE_CONTROL, "private, max-age=0, must-revalidate");
        if (contentLength >= 0) {
            response.setContentLengthLong(contentLength);
        }
        response.setBufferSize(BUFFER_SIZE);

        try (InputStream input = clientResponse.getBody()) {
            OutputStream output = response.getOutputStream();
            byte[] buffer = new byte[BUFFER_SIZE];
            long total = 0;
            int read;
            while ((read = input.read(buffer)) != -1) {
                total += read;
                if (total > properties.downloadMaxBytes()) {
                    throw new DownloadTooLargeException("下载文件超过大小限制");
                }
                output.write(buffer, 0, read);
            }
            output.flush();
        } catch (IOException exception) {
            if (response.isCommitted()) {
                log.info("客户端中断下载 requestId={} filename={}", requestId, filename);
                return;
            }
            throw new UpstreamAgentException(502, "下载流中断", requestId);
        }
    }

    private MediaType mediaTypeFor(String filename) {
        return filename.toLowerCase(Locale.ROOT).endsWith(".docx")
                ? DOCX_MEDIA_TYPE
                : XLSX_MEDIA_TYPE;
    }

    private void validateFilename(String filename) {
        if (filename == null
                || filename.isBlank()
                || filename.contains("..")
                || !SAFE_FILENAME.matcher(filename).matches()) {
            throw new InvalidDownloadNameException();
        }
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
            // 非 JSON 错误体只返回通用提示。
        }
        return "Python Agent 调用失败，HTTP " + status;
    }
}
