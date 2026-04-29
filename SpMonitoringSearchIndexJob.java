package com.ubs.spasa.batch.jobs;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.batch.core.Job;
import org.springframework.batch.core.JobExecution;
import org.springframework.batch.core.Step;
import org.springframework.batch.core.configuration.annotation.EnableBatchProcessing;
import org.springframework.batch.core.configuration.annotation.StepScope;
import org.springframework.batch.core.configuration.annotation.JobBuilderFactory;
import org.springframework.batch.core.configuration.annotation.StepBuilderFactory;
import org.springframework.batch.core.launch.support.RunIdIncrementer;
import org.springframework.batch.core.listener.JobExecutionListenerSupport;
import org.springframework.batch.item.database.JdbcCursorItemReader;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.context.annotation.ComponentScans;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.FilterType;
import org.springframework.context.annotation.Import;


import javax.sql.DataSource;

/**
 * Spring Batch job definition for SP Monitoring Lucene Search Index creation.
 * <p>
 * This is the main entry point for the batch pipeline:
 * <ol>
 *   <li>Reads SP monitoring data from the database (JDBC cursor).</li>
 *   <li>Writes the data into a Lucene search index via {@code SPMonitoringIndexDataWriter}.</li>
 * </ol>
 * Triggered externally by AutoSys — no internal scheduling.
 * </p>
 *
 * @see SpMonitoringLuceneConfig for Lucene infrastructure beans used by this job.
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
public class SpMonitoringSearchIndexJob {

    private static final Logger LOGGER = LoggerFactory.getLogger(SpMonitoringSearchIndexJob.class);

    private static final String ITEM_READER_SQL =
            "SELECT t1.product_id, t1.product_name, t1.monitoring_status, " +
            "       t1.last_updated, t2.universe_value AS SPASA_UNIVERSE " +
            "FROM spasa_pymon_data.mv_pymon_tableau_data t1 " +
            "JOIN spasa_ods.tbl_product_universe t2 " +
            "ON t1.product_id = t2.product_id";
    // ^^^ TODO: Replace the column list above with the actual columns
    //     mapped by SPMonitoringBeanRowMapper. Avoid SELECT t1.* in batch jobs.

    private final JobBuilderFactory jobBuilderFactory;
    private final StepBuilderFactory stepBuilderFactory;
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
    public SpMonitoringSearchIndexJob(
            JobBuilderFactory jobBuilderFactory,
            StepBuilderFactory stepBuilderFactory,
            DataSource dataSource,
            @Qualifier("stepExceptionsRecorder") StepExecutionListener stepExceptionsRecorder,
            @Qualifier("jobStatsRecorder") StepExecutionListener jobStatsRecorder,
            @Qualifier("jobMailNotificationSender") JobExecutionListenerSupport jobMailNotificationSender,
            SPMonitoringBeanRowMapper spMonitoringBeanRowMapper,
            SPMonitoringIndexDataWriter spMonitoringIndexDataWriter,
            SPMonitoringIndexJobParamValidator spMonitoringIndexJobParamValidator) {
        this.jobBuilderFactory = jobBuilderFactory;
        this.stepBuilderFactory = stepBuilderFactory;
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
        return stepBuilderFactory.get("spMonitoringIndexTask")
                .<SPMonitoringBean, SPMonitoringBean>chunk(chunkSize)
                .reader(itemReader())
                .writer(spMonitoringIndexDataWriter)
                .listener(stepExceptionsRecorder)
                .listener(jobStatsRecorder)
                .build();
    }

    // ── Job ───────────────────────────────────────────────────────────────────

    @Bean
    public Job spMonitoringSearchIndexJob() {
        return jobBuilderFactory.get(JobNames.SP_MONITORING_SEARCH_INDEX_JOB)
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
