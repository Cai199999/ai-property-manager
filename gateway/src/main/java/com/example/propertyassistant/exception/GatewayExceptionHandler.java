package com.example.propertyassistant.exception;

import com.example.propertyassistant.config.RequestIdFilter;
import com.example.propertyassistant.dto.GatewayErrorResponse;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.validation.FieldError;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import org.springframework.web.multipart.MaxUploadSizeExceededException;
import org.springframework.web.multipart.support.MissingServletRequestPartException;
import org.springframework.web.servlet.resource.NoResourceFoundException;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@RestControllerAdvice
public class GatewayExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GatewayExceptionHandler.class);

    @ExceptionHandler(UpstreamAgentException.class)
    public ResponseEntity<GatewayErrorResponse> handleUpstream(
            UpstreamAgentException exception,
            HttpServletRequest request
    ) {
        HttpStatus status = resolveStatus(exception.getUpstreamStatus());
        String requestId = requestId(request, exception.getRequestId());
        GatewayErrorResponse body = new GatewayErrorResponse(
                requestId,
                new GatewayErrorResponse.ErrorBody(
                        "UPSTREAM_SERVICE_ERROR",
                        exception.getMessage(),
                        Map.of("upstream_status", exception.getUpstreamStatus())
                )
        );
        return ResponseEntity.status(status).body(body);
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<GatewayErrorResponse> handleValidation(
            MethodArgumentNotValidException exception,
            HttpServletRequest request
    ) {
        List<Map<String, String>> errors = exception.getBindingResult()
                .getFieldErrors()
                .stream()
                .map(this::formatFieldError)
                .toList();
        String requestId = requestId(request, null);
        Map<String, Object> details = new LinkedHashMap<>();
        details.put("errors", errors);
        GatewayErrorResponse body = new GatewayErrorResponse(
                requestId,
                new GatewayErrorResponse.ErrorBody(
                        "REQUEST_VALIDATION_FAILED",
                        "请求参数校验失败",
                        details
                )
        );
        return ResponseEntity.badRequest().body(body);
    }

    @ExceptionHandler(MethodArgumentTypeMismatchException.class)
    public ResponseEntity<GatewayErrorResponse> handleTypeMismatch(
            MethodArgumentTypeMismatchException exception,
            HttpServletRequest request
    ) {
        String requestId = requestId(request, null);
        GatewayErrorResponse body = new GatewayErrorResponse(
                requestId,
                new GatewayErrorResponse.ErrorBody("BAD_REQUEST", "请求参数类型错误")
        );
        return ResponseEntity.badRequest().body(body);
    }

    @ExceptionHandler(InvalidUploadException.class)
    public ResponseEntity<GatewayErrorResponse> handleInvalidUpload(
            InvalidUploadException exception,
            HttpServletRequest request
    ) {
        return error(
                request,
                HttpStatus.BAD_REQUEST,
                "UPLOAD_INVALID",
                exception.getMessage()
        );
    }

    @ExceptionHandler(UploadTooLargeException.class)
    public ResponseEntity<GatewayErrorResponse> handleUploadTooLarge(
            UploadTooLargeException exception,
            HttpServletRequest request
    ) {
        return error(
                request,
                HttpStatus.PAYLOAD_TOO_LARGE,
                "UPLOAD_TOO_LARGE",
                exception.getMessage()
        );
    }

    @ExceptionHandler(MaxUploadSizeExceededException.class)
    public ResponseEntity<GatewayErrorResponse> handleMultipartTooLarge(
            MaxUploadSizeExceededException exception,
            HttpServletRequest request
    ) {
        return error(
                request,
                HttpStatus.PAYLOAD_TOO_LARGE,
                "UPLOAD_TOO_LARGE",
                "上传文件超过大小限制"
        );
    }

    @ExceptionHandler(MissingServletRequestPartException.class)
    public ResponseEntity<GatewayErrorResponse> handleMissingPart(
            MissingServletRequestPartException exception,
            HttpServletRequest request
    ) {
        return error(
                request,
                HttpStatus.BAD_REQUEST,
                "UPLOAD_INVALID",
                "缺少上传文件字段 file"
        );
    }

    @ExceptionHandler(InvalidDownloadNameException.class)
    public ResponseEntity<GatewayErrorResponse> handleInvalidDownloadName(
            InvalidDownloadNameException exception,
            HttpServletRequest request
    ) {
        return error(
                request,
                HttpStatus.BAD_REQUEST,
                "DOWNLOAD_NAME_INVALID",
                exception.getMessage()
        );
    }

    @ExceptionHandler(DownloadBusyException.class)
    public ResponseEntity<GatewayErrorResponse> handleDownloadBusy(
            DownloadBusyException exception,
            HttpServletRequest request
    ) {
        String requestId = requestId(request, null);
        GatewayErrorResponse body = new GatewayErrorResponse(
                requestId,
                new GatewayErrorResponse.ErrorBody(
                        "DOWNLOAD_BUSY",
                        exception.getMessage(),
                        Map.of("retry_after_seconds", 5)
                )
        );
        return ResponseEntity.status(HttpStatus.TOO_MANY_REQUESTS)
                .header(HttpHeaders.RETRY_AFTER, "5")
                .body(body);
    }

    @ExceptionHandler(DownloadTooLargeException.class)
    public ResponseEntity<GatewayErrorResponse> handleDownloadTooLarge(
            DownloadTooLargeException exception,
            HttpServletRequest request
    ) {
        return error(
                request,
                HttpStatus.PAYLOAD_TOO_LARGE,
                "DOWNLOAD_TOO_LARGE",
                exception.getMessage()
        );
    }

    /**
     * 静态资源不存在时返回 404，而不是让兜底处理器包装成 500。
     *
     * <p>浏览器会主动请求 {@code /favicon.ico} 等资源，缺失属于正常情况，
     * 不应污染网关错误日志，也不应让前端误以为后端故障。</p>
     */
    @ExceptionHandler(NoResourceFoundException.class)
    public ResponseEntity<Void> handleResourceNotFound(
            NoResourceFoundException exception,
            HttpServletRequest request
    ) {
        log.debug(
                "静态资源不存在 path={} requestId={}",
                exception.getResourcePath(),
                requestId(request, null)
        );
        return ResponseEntity.status(HttpStatus.NOT_FOUND).build();
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<GatewayErrorResponse> handleUnexpected(
            Exception exception,
            HttpServletRequest request
    ) {
        String requestId = requestId(request, null);
        log.error("网关未处理异常 requestId={}", requestId, exception);
        GatewayErrorResponse body = new GatewayErrorResponse(
                requestId,
                new GatewayErrorResponse.ErrorBody("GATEWAY_INTERNAL_ERROR", "网关内部错误")
        );
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(body);
    }

    private ResponseEntity<GatewayErrorResponse> error(
            HttpServletRequest request,
            HttpStatus status,
            String code,
            String message
    ) {
        String requestId = requestId(request, null);
        GatewayErrorResponse body = new GatewayErrorResponse(
                requestId,
                new GatewayErrorResponse.ErrorBody(code, message)
        );
        return ResponseEntity.status(status).body(body);
    }

    private Map<String, String> formatFieldError(FieldError error) {
        Map<String, String> detail = new LinkedHashMap<>();
        detail.put("field", error.getField());
        detail.put("message", error.getDefaultMessage() == null ? "参数不合法" : error.getDefaultMessage());
        return detail;
    }

    private HttpStatus resolveStatus(int status) {
        HttpStatus resolved = HttpStatus.resolve(status);
        return resolved == null ? HttpStatus.BAD_GATEWAY : resolved;
    }

    private String requestId(HttpServletRequest request, String fallback) {
        Object attribute = request.getAttribute(RequestIdFilter.ATTRIBUTE_NAME);
        if (attribute instanceof String value && !value.isBlank()) {
            return value;
        }
        return fallback == null ? "unknown" : fallback;
    }
}
