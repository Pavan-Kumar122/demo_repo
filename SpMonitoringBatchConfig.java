package com.ubs.spasa.batch.jobs;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.batch.core.Job;
import org.springframework.batch.core.JobExecution;
import org.springframework.batch.core.Step;
import org.springframework.batch.core.configuration.annotation.EnableBatchProcessing;
import org.springframework.batch.core.configuration.annotation.StepScope;
import org.springframework.batch.core.job.builder.JobBuilder;
import org.springframework.batch.core.launch.support.RunIdIncrementer;
import org.springframework.batch.core.listener.JobExecutionListenerSupport;
import org.springframework.batch.core.repository.JobRepository;
import org.springframework.batch.core.step.builder.StepBuilder;
import org.springframework.batch.item.database.JdbcCursorItemReader;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.context.annotation.ComponentScans;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.FilterType;
import org.springframework.context.annotation.Import;
import org.springframework.transaction.PlatformTransactionManager;

import javax.sql.DataSource;

/**
 * Spring Batch job and step configuration for SP Monitoring Search Index.
 * <p>
 * Responsibilities:
 * - Defines the batch {@link Job} and its {@link Step}.
 * - Configures the JDBC {@link JdbcCursorItemReader} (step-scoped).
 * - Wires step-level and job-level listeners.
 * </p>
 */
@Configuration
@EnableBatchProcessing
@Import(IndexingJobConfiguration.class)
@ComponentScans({
        @ComponentScan("com.ubs.spasa.batch.core"),
        @ComponentScan("com.ubs.spasa.batch.spmonitoringsearch"),
        @ComponentScan("com.ubs.spasa.spmonitoringsearch"),
        @ComponentScan(
                value = "com.ubs.spasa.productsearch.lucene",
                excludeFilters = @ComponentScan.Filter(
                        type = FilterType.ASSIGNABLE_TYPE,
                        classes = {PendingIndexUpdateTasklet.class, ProductSearchIndexUpdateAspect.class}
                )
        ),
        @ComponentScan("com.ubs.spasa.util"),
        @ComponentScan("com.ubs.spasa.pi")
})
public class SpMonitoringBatchConfig {

    private static final Logger LOGGER = LoggerFactory.getLogger(SpMonitoringBatchConfig.class);

    private static final String ITEM_READER_SQL =;
    // ^^^ TODO: Replace the column list above with the actual columns
    //     mapped by SPMonitoringBeanRowMapper. Avoid SELECT t1.* in batch jobs.

    private final JobRepository jobRepository;
    private final PlatformTransactionManager transactionManager;
    private final DataSource dataSource;
    private final StepExecutionListener stepExceptionsRecorder;
    private final StepExecutionListener jobStatsRecorder;
    private final JobExecutionListenerSupport jobMailNotificationSender;
    private final SPMonitoringBeanRowMapper spMonitoringBeanRowMapper;
    private final SPMonitoringIndexDataWriter spMonitoringIndexDataWriter;
    private final SPMonitoringIndexJobParamValidator spMonitoringIndexJobParamValidator;

    @Value("${sp.monitoring.batch.chunk-size:5000}")
    private int chunkSize;

    @Value("${sp.monitoring.batch.fetch-size:5000}")
    private int fetchSize;

    /**
     * Explicit constructor — ensures {@code @Qualifier} annotations are
     * honoured without requiring a {@code lombok.config} with
     * {@code copyableAnnotations}.
     */
    public SpMonitoringBatchConfig(
            JobRepository jobRepository,
            PlatformTransactionManager transactionManager,
            DataSource dataSource,
            @Qualifier("stepExceptionsRecorder") StepExecutionListener stepExceptionsRecorder,
            @Qualifier("jobStatsRecorder") StepExecutionListener jobStatsRecorder,
            @Qualifier("jobMailNotificationSender") JobExecutionListenerSupport jobMailNotificationSender,
            SPMonitoringBeanRowMapper spMonitoringBeanRowMapper,
            SPMonitoringIndexDataWriter spMonitoringIndexDataWriter,
            SPMonitoringIndexJobParamValidator spMonitoringIndexJobParamValidator) {
        this.jobRepository = jobRepository;
        this.transactionManager = transactionManager;
        this.dataSource = dataSource;
        this.stepExceptionsRecorder = stepExceptionsRecorder;
        this.jobStatsRecorder = jobStatsRecorder;
        this.jobMailNotificationSender = jobMailNotificationSender;
        this.spMonitoringBeanRowMapper = spMonitoringBeanRowMapper;
        this.spMonitoringIndexDataWriter = spMonitoringIndexDataWriter;
        this.spMonitoringIndexJobParamValidator = spMonitoringIndexJobParamValidator;
    }

    // ── Item Reader (step-scoped) ─────────────────────────────────────────────

    @Bean
    @StepScope
    public JdbcCursorItemReader<SPMonitoringBean> itemReader() {
        JdbcCursorItemReader<SPMonitoringBean> reader = new JdbcCursorItemReader<>();
        reader.setDataSource(dataSource);
        reader.setSql(ITEM_READER_SQL);
        reader.setRowMapper(spMonitoringBeanRowMapper);
        reader.setFetchSize(fetchSize);
        return reader;
    }

    // ── Step ──────────────────────────────────────────────────────────────────

    @Bean
    public Step spMonitoringIndexTask() {
        return new StepBuilder("spMonitoringIndexTask", jobRepository)
                .<SPMonitoringBean, SPMonitoringBean>chunk(chunkSize, transactionManager)
                .reader(itemReader())
                .writer(spMonitoringIndexDataWriter)
                .listener(stepExceptionsRecorder)
                .listener(jobStatsRecorder)
                .build();
    }

    // ── Job ───────────────────────────────────────────────────────────────────

    @Bean
    public Job spMonitoringSearchIndexJob() {
        return new JobBuilder(JobNames.SP_MONITORING_SEARCH_INDEX_JOB, jobRepository)
                .incrementer(new RunIdIncrementer())
                .validator(spMonitoringIndexJobParamValidator)
                .start(spMonitoringIndexTask())
                .listener(jobMailNotificationSender)
                .listener(jobLoggingListener())
                .build();
    }

    /**
     * Lightweight listener that logs job start/end for diagnostics.
     */
    private JobExecutionListenerSupport jobLoggingListener() {
        return new JobExecutionListenerSupport() {
            @Override
            public void beforeJob(JobExecution jobExecution) {
                LOGGER.info("Starting SP Monitoring Index Job [id={}]", jobExecution.getId());
            }

            @Override
            public void afterJob(JobExecution jobExecution) {
                LOGGER.info("Finished SP Monitoring Index Job [id={}, status={}]",
                        jobExecution.getId(), jobExecution.getStatus());
            }
        };
    }
}
