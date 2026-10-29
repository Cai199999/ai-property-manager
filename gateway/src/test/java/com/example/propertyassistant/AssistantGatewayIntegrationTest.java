package com.example.propertyassistant;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.core.io.ByteArrayResource;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 启动真实 Tomcat + 本地假 Python 服务，验证网关转发链路。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class AssistantGatewayIntegrationTest {

    private static final String RESPONSE_JSON = """
            {
              "request_id": "gateway-test-001",
              "query": "汇总上个月工单",
              "data_source": "rpa",
              "time_range": {"start_date": "2026-09-01", "end_date": "2026-09-30"},
              "parser": {
                "engine": "rule_based",
                "model": null,
                "confidence": 0.97,
                "reason": "上个月"
              },
              "summary": {
                "total_records": 90,
                "total_amount": 12345.67,
                "completed_count": 70,
                "completion_rate": 0.7778,
                "average_satisfaction": 4.2,
                "by_project": []
              },
              "records_preview": [{"工单编号": "WO001"}],
              "meta": {
                "fetched_at": "2026-10-29T12:00:00+08:00",
                "elapsed_ms": 42,
                "row_count": 90,
                "warnings": []
              }
            }
            """;

    private static final String CODE_RESPONSE_JSON = """
            {
              "request_id": "gateway-code-001",
              "query": "按区域统计工单量",
              "data_source": "directory",
              "time_range": {"start_date": "2026-09-01", "end_date": "2026-09-30"},
              "parser": {"engine": "deepseek", "model": "deepseek-chat", "confidence": 0.99},
              "result": {
                "columns": [{"name": "所属区域", "dtype": "object"}, {"name": "工单量", "dtype": "int64"}],
                "rows": [{"所属区域": "朝阳区", "工单量": 132}],
                "row_count": 1
              },
              "chart_meta": {
                "type": "bar",
                "title": "工单量按所属区域",
                "x": "所属区域",
                "y": ["工单量"],
                "series": [{"name": "工单量", "key": "工单量", "unit": "单"}]
              },
              "download_url": "/api/v1/assistant/download/report_0123456789abcdef0123456789abcdef.xlsx",
              "code": {"attempts": 1, "retry_errors": []},
              "meta": {"warnings": [], "future_field": {"enabled": true}}
            }
            """;

    private static final String TABLES_RESPONSE_JSON = """
            {
              "tables": [
                {"name": "工单表", "role": "fact", "row_count": 150, "file_name": "工单表.xlsx"}
              ],
              "primary_table": "工单表",
              "warnings": []
            }
            """;

    private static final String UPLOAD_RESPONSE_JSON = """
            {
              "tables": [
                {"name": "园区项目表", "role": "dimension", "row_count": 8, "file_name": "园区项目表.xlsx"}
              ],
              "primary_table": "园区项目表",
              "warnings": [],
              "imported": {"name": "园区项目表", "row_count": 8, "column_count": 9}
            }
            """;

    private static final String PREVIEW_RESPONSE_JSON = """
            {
              "name": "工单表_A区",
              "file_name": "工单表_A区.xlsx",
              "role": "fact",
              "schema_group": "work_orders",
              "total_rows": 420,
              "offset": 0,
              "limit": 2,
              "has_more": true,
              "columns": [{"name": "单号", "dtype": "object", "null_count": 0}],
              "rows": [{"单号": "A-001"}],
              "derived_columns": ["费用金额"]
            }
            """;

    private static final byte[] DOWNLOAD_BYTES = new byte[256 * 1024];

    static {
        for (int index = 0; index < DOWNLOAD_BYTES.length; index++) {
            DOWNLOAD_BYTES[index] = (byte) (index % 251);
        }
    }

    private static final AtomicReference<String> FORWARDED_REQUEST_ID = new AtomicReference<>();
    private static final AtomicReference<String> FORWARDED_BODY = new AtomicReference<>();
    private static final AtomicReference<String> FORWARDED_UPLOAD_CONTENT_TYPE =
            new AtomicReference<>();
    private static final AtomicReference<String> FORWARDED_UPLOAD_BODY = new AtomicReference<>();
    private static final AtomicReference<String> FORWARDED_PREVIEW_QUERY =
            new AtomicReference<>();
    private static final HttpServer UPSTREAM;
    private static final ExecutorService UPSTREAM_EXECUTOR =
            Executors.newVirtualThreadPerTaskExecutor();

    static {
        try {
            UPSTREAM = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            UPSTREAM.createContext("/api/v1/assistant/query", exchange -> {
                FORWARDED_REQUEST_ID.set(exchange.getRequestHeaders().getFirst("X-Request-Id"));
                String requestBody = new String(
                        exchange.getRequestBody().readAllBytes(),
                        StandardCharsets.UTF_8
                );
                FORWARDED_BODY.set(requestBody);

                boolean upstreamError = requestBody.contains("触发上游错误");
                int status = upstreamError ? 422 : 200;
                String responseBody = upstreamError ? """
                        {
                          "request_id": "gateway-test-001",
                          "error": {
                            "code": "TIME_RANGE_INVALID",
                            "message": "无法从请求中识别有效的数据时间范围",
                            "details": {}
                          }
                        }
                        """ : RESPONSE_JSON;

                byte[] response = responseBody.getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().add("Content-Type", "application/json");
                exchange.sendResponseHeaders(status, response.length);
                try (OutputStream output = exchange.getResponseBody()) {
                    output.write(response);
                }
            });
            UPSTREAM.createContext("/api/v1/assistant/query/code", exchange -> {
                FORWARDED_REQUEST_ID.set(exchange.getRequestHeaders().getFirst("X-Request-Id"));
                FORWARDED_BODY.set(new String(
                        exchange.getRequestBody().readAllBytes(),
                        StandardCharsets.UTF_8
                ));
                sendJson(exchange, 200, CODE_RESPONSE_JSON);
            });
            UPSTREAM.createContext("/api/v1/assistant/tables/upload", exchange -> {
                FORWARDED_REQUEST_ID.set(exchange.getRequestHeaders().getFirst("X-Request-Id"));
                FORWARDED_UPLOAD_CONTENT_TYPE.set(
                        exchange.getRequestHeaders().getFirst("Content-Type")
                );
                FORWARDED_UPLOAD_BODY.set(new String(
                        exchange.getRequestBody().readAllBytes(),
                        StandardCharsets.ISO_8859_1
                ));
                sendJson(exchange, 200, UPLOAD_RESPONSE_JSON);
            });
            UPSTREAM.createContext("/api/v1/assistant/tables/preview", exchange -> {
                FORWARDED_PREVIEW_QUERY.set(exchange.getRequestURI().getRawQuery());
                sendJson(exchange, 200, PREVIEW_RESPONSE_JSON);
            });
            UPSTREAM.createContext("/api/v1/assistant/tables", exchange ->
                    sendJson(exchange, 200, TABLES_RESPONSE_JSON));
            UPSTREAM.createContext("/api/v1/assistant/download/", exchange -> {
                String path = exchange.getRequestURI().getPath();
                String filename = path.substring(path.lastIndexOf('/') + 1);
                if ("missing.xlsx".equals(filename)) {
                    sendJson(exchange, 404, """
                            {"error": {"code": "REPORT_NOT_FOUND", "message": "下载文件不存在或已过期"}}
                            """);
                    return;
                }
                exchange.getResponseHeaders().add(
                        "Content-Type",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                );
                exchange.sendResponseHeaders(200, DOWNLOAD_BYTES.length);
                try (OutputStream output = exchange.getResponseBody()) {
                    output.write(DOWNLOAD_BYTES);
                }
            });
            UPSTREAM.setExecutor(UPSTREAM_EXECUTOR);
            UPSTREAM.start();
        } catch (IOException exception) {
            throw new IllegalStateException("无法启动测试上游服务", exception);
        }
    }

    @DynamicPropertySource
    static void agentProperties(DynamicPropertyRegistry registry) {
        registry.add(
                "gateway.agent.base-url",
                () -> "http://127.0.0.1:" + UPSTREAM.getAddress().getPort()
        );
    }

    @Autowired
    private TestRestTemplate restTemplate;

    @Autowired
    private ObjectMapper objectMapper;

    @AfterAll
    static void stopUpstream() {
        UPSTREAM.stop(0);
        UPSTREAM_EXECUTOR.close();
    }

    private static void sendJson(
            com.sun.net.httpserver.HttpExchange exchange,
            int status,
            String body
    ) throws IOException {
        byte[] response = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().add("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, response.length);
        try (OutputStream output = exchange.getResponseBody()) {
            output.write(response);
        }
    }

    @Test
    void forwardsRequestAndReturnsAgentResponse() throws Exception {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        headers.set("X-Request-Id", "gateway-test-001");

        ResponseEntity<String> response = restTemplate.exchange(
                "/api/v1/assistant/query",
                HttpMethod.POST,
                new HttpEntity<>(
                        Map.of("message", "汇总上个月工单", "property_name", "樱花苑"),
                        headers
                ),
                String.class
        );

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(response.getHeaders().get("X-Request-Id"))
                .containsExactly("gateway-test-001");
        assertThat(FORWARDED_REQUEST_ID.get()).isEqualTo("gateway-test-001");
        assertThat(FORWARDED_BODY.get()).contains("\"property_name\":\"樱花苑\"");

        JsonNode body = objectMapper.readTree(response.getBody());
        assertThat(body.path("summary").path("total_records").asInt()).isEqualTo(90);
        assertThat(body.path("time_range").path("start_date").asText()).isEqualTo("2026-09-01");
        assertThat(body.path("data_source").asText()).isEqualTo("rpa");
    }

    @Test
    void relaysUpstreamClientErrorWithUnifiedBody() {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);

        ResponseEntity<String> response = restTemplate.exchange(
                "/api/v1/assistant/query",
                HttpMethod.POST,
                new HttpEntity<>(Map.of("message", "触发上游错误"), headers),
                String.class
        );

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.UNPROCESSABLE_ENTITY);
        assertThat(response.getBody()).contains("UPSTREAM_SERVICE_ERROR");
        assertThat(response.getBody()).contains("无法从请求中识别有效的数据时间范围");
        assertThat(response.getHeaders().get("X-Request-Id")).hasSize(1);
    }

    @Test
    void rejectsInvalidMessageBeforeCallingAgent() {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);

        ResponseEntity<String> response = restTemplate.exchange(
                "/api/v1/assistant/query",
                HttpMethod.POST,
                new HttpEntity<>(Map.of("message", " "), headers),
                String.class
        );

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(response.getBody()).contains("REQUEST_VALIDATION_FAILED");
    }

    @Test
    void forwardsDynamicCodeResponseWithoutDroppingUnknownFields() throws Exception {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        headers.set("X-Request-Id", "gateway-code-001");

        ResponseEntity<String> response = restTemplate.exchange(
                "/api/v1/assistant/query/code",
                HttpMethod.POST,
                new HttpEntity<>(Map.of("message", "按区域统计工单量"), headers),
                String.class
        );

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(FORWARDED_REQUEST_ID.get()).isEqualTo("gateway-code-001");
        assertThat(FORWARDED_BODY.get()).contains("按区域统计工单量");

        JsonNode body = objectMapper.readTree(response.getBody());
        assertThat(body.path("chart_meta").path("type").asText()).isEqualTo("bar");
        assertThat(body.path("chart_meta").path("series").get(0).path("unit").asText())
                .isEqualTo("单");
        assertThat(body.path("download_url").asText())
                .startsWith("/api/v1/assistant/download/report_");
        assertThat(body.path("meta").path("future_field").path("enabled").asBoolean())
                .isTrue();
        assertThat(body.path("result").path("rows").get(0).path("工单量").asInt())
                .isEqualTo(132);
    }

    @Test
    void proxiesTableCatalogAndMultipartUpload() {
        ResponseEntity<String> tables = restTemplate.getForEntity(
                "/api/v1/assistant/tables",
                String.class
        );
        assertThat(tables.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(tables.getBody()).contains("工单表");

        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.MULTIPART_FORM_DATA);
        headers.set("X-Request-Id", "gateway-upload-001");
        MultiValueMap<String, Object> body = new LinkedMultiValueMap<>();
        body.add("file", new ByteArrayResource(
                "fake-xlsx-content-a".getBytes(StandardCharsets.UTF_8)
        ) {
            @Override
            public String getFilename() {
                return "工单表_A区.xlsx";
            }
        });
        body.add("file", new ByteArrayResource(
                "fake-xlsx-content-b".getBytes(StandardCharsets.UTF_8)
        ) {
            @Override
            public String getFilename() {
                return "工单表_B区.xlsx";
            }
        });

        ResponseEntity<String> upload = restTemplate.exchange(
                "/api/v1/assistant/tables/upload",
                HttpMethod.POST,
                new HttpEntity<>(body, headers),
                String.class
        );

        assertThat(upload.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(FORWARDED_REQUEST_ID.get()).isEqualTo("gateway-upload-001");
        assertThat(FORWARDED_UPLOAD_CONTENT_TYPE.get()).startsWith("multipart/form-data");
        assertThat(FORWARDED_UPLOAD_BODY.get())
                .contains("fake-xlsx-content-a")
                .contains("fake-xlsx-content-b");
        // 文件段使用 ASCII 安全文件名，原始中文名放到独立 filename 字段，
        // 避免 RFC 2047 encoded-word 让 Python 的 .xlsx 后缀校验失败。
        assertThat(FORWARDED_UPLOAD_BODY.get())
                .contains("filename=\"upload_1.xlsx\"")
                .contains("filename=\"upload_2.xlsx\"")
                .doesNotContain("=?UTF-8?");
        String forwardedUtf8 = new String(
                FORWARDED_UPLOAD_BODY.get().getBytes(StandardCharsets.ISO_8859_1),
                StandardCharsets.UTF_8
        );
        assertThat(forwardedUtf8)
                .contains("name=\"filename\"")
                .contains("工单表_A区.xlsx")
                .contains("工单表_B区.xlsx");
        assertThat(upload.getBody()).contains("园区项目表");
    }

    @Test
    void proxiesTablePreviewWithPagingParameters() {
        ResponseEntity<String> response = restTemplate.getForEntity(
                "/api/v1/assistant/tables/preview?name={name}&offset=0&limit=2",
                String.class,
                "工单表_A区"
        );

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(response.getBody())
                .contains("工单表_A区")
                .contains("derived_columns");
        assertThat(FORWARDED_PREVIEW_QUERY.get())
                .contains("name=")
                .contains("offset=0")
                .contains("limit=2");
    }

    @Test
    void streamsDownloadAndKeepsFilenameHeaders() {
        ResponseEntity<byte[]> response = restTemplate.getForEntity(
                "/api/v1/assistant/download/report_0123456789abcdef0123456789abcdef.xlsx",
                byte[].class
        );

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(response.getBody()).isEqualTo(DOWNLOAD_BYTES);
        assertThat(response.getHeaders().getFirst(HttpHeaders.CONTENT_DISPOSITION))
                .contains("attachment")
                .contains("report_0123456789abcdef0123456789abcdef.xlsx");
        assertThat(response.getHeaders().getFirst(HttpHeaders.CACHE_CONTROL))
                .contains("private");
        assertThat(response.getHeaders().getFirst("X-Content-Type-Options"))
                .isEqualTo("nosniff");
    }

    @Test
    void rejectsInvalidDownloadNameBeforeCallingAgent() {
        ResponseEntity<String> response = restTemplate.getForEntity(
                "/api/v1/assistant/download/evil.txt",
                String.class
        );

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(response.getBody()).contains("DOWNLOAD_NAME_INVALID");
    }

    @Test
    void relaysUpstreamDownloadNotFoundAsJson() {
        ResponseEntity<String> response = restTemplate.getForEntity(
                "/api/v1/assistant/download/missing.xlsx",
                String.class
        );

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
        assertThat(response.getBody()).contains("下载文件不存在或已过期");
    }
}
