package com.example.propertyassistant.config;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

import java.net.http.HttpClient;
import java.time.Duration;

@Configuration
@EnableConfigurationProperties(GatewayProperties.class)
public class RestClientConfig {

    /**
     * 普通 JSON 请求客户端：连接与读取超时较短，避免 Agent 卡死拖垮网关线程池。
     */
    @Bean
    @Qualifier("agentRestClient")
    public RestClient agentRestClient(
            RestClient.Builder restClientBuilder,
            GatewayProperties properties
    ) {
        return buildClient(restClientBuilder, properties, properties.readTimeout());
    }

    /**
     * 下载专用客户端：读取超时更长，但仍然禁止跟随重定向，防止绕过固定上游地址。
     */
    @Bean
    @Qualifier("agentDownloadRestClient")
    public RestClient agentDownloadRestClient(
            RestClient.Builder restClientBuilder,
            GatewayProperties properties
    ) {
        return buildClient(restClientBuilder, properties, properties.downloadReadTimeout());
    }

    private RestClient buildClient(
            RestClient.Builder restClientBuilder,
            GatewayProperties properties,
            Duration readTimeout
    ) {
        HttpClient httpClient = HttpClient.newBuilder()
                .connectTimeout(properties.connectTimeout())
                .followRedirects(HttpClient.Redirect.NEVER)
                .version(HttpClient.Version.HTTP_1_1)
                .build();

        JdkClientHttpRequestFactory requestFactory = new JdkClientHttpRequestFactory(httpClient);
        requestFactory.setReadTimeout(readTimeout);

        // 复用 Boot 配置过的 Builder，确保 spring.jackson 的 SNAKE_CASE 对出入参都生效。
        return restClientBuilder.clone()
                .baseUrl(properties.baseUrl())
                .requestFactory(requestFactory)
                .build();
    }
}
