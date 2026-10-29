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
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

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

    private static final AtomicReference<String> FORWARDED_REQUEST_ID = new AtomicReference<>();
    private static final AtomicReference<String> FORWARDED_BODY = new AtomicReference<>();
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
}
