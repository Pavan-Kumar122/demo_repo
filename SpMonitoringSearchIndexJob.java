package com.ubs.spasa.batch.jobs;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.batch.core.Job;
import org.springframework.batch.core.JobExecution;
import org.springframework.batch.core.JobExecutionListener;
import org.springframework.batch.core.Step;
import org.springframework.batch.core.StepExecutionListener;
import org.springframework.batch.core.configuration.annotation.EnableBatchProcessing;
import org.springframework.batch.core.configuration.annotation.JobBuilderFactory;
import org.springframework.batch.core.configuration.annotation.StepBuilderFactory;
import org.springframework.batch.core.configuration.annotation.StepScope;
import org.springframework.batch.core.launch.support.RunIdIncrementer;
import org.springframework.batch.core.listener.JobExecutionListenerSupport;
import org.springframework.batch.item.ItemWriter;
import org.springframework.batch.item.database.JdbcCursorItemReader;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.context.annotation.ComponentScans;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.FilterType;
import org.springframework.context.annotation.Import;
import org.springframework.context.annotation.ImportResource;
import org.springframework.context.annotation.Lazy;

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
@Lazy
@Configuration("spMonitoringSearchIndexJobConfig")
@ImportResource("classpath:/infra/batch-mail-util.xml")
@EnableBatchProcessing
@Import(IndexingJobConfiguration.class)
@ComponentScans({
        @ComponentScan("com.ubs.spasa.batch.core"),
        @ComponentScan("com.ubs.spasa.batch.spmonitoringsearch"),
        @ComponentScan("com.ubs.spasa.spmonitoringsearch"),
        @ComponentScan(
                value = "com.ubs.spasa.productsearch.lucene",
                excludeFilters = @ComponentScan.Filter(
                        type = FilterType.REGEX,
                        pattern = "com\\.ubs\\.spasa\\.productsearch\\.lucene\\.(PendingIndexUpdateTasklet|ProductSearchIndexUpdateAspect)"
                )
        ),
        @ComponentScan("com.ubs.spasa.util"),
        @ComponentScan("com.ubs.spasa.pi")
})
public class SpMonitoringSearchIndexJob {

    private static final Logger LOGGER = LoggerFactory.getLogger(SpMonitoringSearchIndexJob.class);

    @Autowired
    private JobBuilderFactory jobs;

    @Autowired
    private StepBuilderFactory steps;

    @Autowired
    private DataSource dataSource;

    /* Beans provided by batch-mail-util.xml (imported above) */
    @Autowired
    @Qualifier("stepExceptionsRecorder")
    private StepExecutionListener stepExceptionsRecorder;

    @Autowired
    @Qualifier("jobStatsRecorder")
    private StepExecutionListener jobStatsRecorder;

    @Autowired
    @Qualifier("jobMailNotificationSender")
    private JobExecutionListener jobMailNotificationSender;

    @Value("${sp.monitoring.batch.chunk-size:5000}")
    private int chunkSize;

    @Value("${sp.monitoring.batch.fetch-size:5000}")
    private int fetchSize;

    // ── Item Reader (step-scoped) ─────────────────────────────────────────────

    @Bean
    @StepScope
    public JdbcCursorItemReader<SPMonitoringBean> itemReader() {
        JdbcCursorItemReader<SPMonitoringBean> reader = new JdbcCursorItemReader<>();
        reader.setDataSource(dataSource);
        reader.setSql(
                "SELECT t1.*, t2.universe_value as SPASA_UNIVERSE " +
                "FROM spasa_pymon_data.mv_pymon_tableau_data t1 " +
                "JOIN spasa_ods.tbl_product_universe t2 " +
                "ON t1.product_id = t2.product_id"
        );
        reader.setRowMapper(spMonitoringBeanRowMapper());
        reader.setFetchSize(fetchSize);
        return reader;
    }

    // ── Step & Job ────────────────────────────────────────────────────────────

    @Bean
    public Step spMonitoringIndexTask() {
        return steps.get("spMonitoringIndexTask")
                .<SPMonitoringBean, SPMonitoringBean>chunk(chunkSize)
                .reader(itemReader())
                .writer((ItemWriter<? super SPMonitoringBean>) spMonitoringIndexDataWriter())
                .listener(stepExceptionsRecorder)      // from batch-mail-util.xml
                .listener(jobStatsRecorder)             // from batch-mail-util.xml
                .build();
    }

    @Bean
    public Job monitoringSearchIndexJob() {
        return jobs.get(JobNames.SP_MONITORING_SEARCH_INDEX_JOB)
                .incrementer(new RunIdIncrementer())
                .validator(spMonitoringIndexJobParamValidator())
                .start(spMonitoringIndexTask())
                .listener(jobMailNotificationSender)    // from batch-mail-util.xml
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

    // ── Job Context Support ───────────────────────────────────────────────────

    @Bean
    public JobContextSupport jobContextSupport() {
        JobContextSupport support = new JobContextSupport();
        support.setJobName(JobNames.SP_MONITORING_SEARCH_INDEX_JOB);
        return support;
    }

    // ── Row Mapper, Writer & Validator (kept here for co-location with Step) ─

    @Bean
    public SPMonitoringBeanRowMapper spMonitoringBeanRowMapper() {
        return new SPMonitoringBeanRowMapper();
    }

    @Bean
    public SPMonitoringIndexDataWriter spMonitoringIndexDataWriter() {
        return new SPMonitoringIndexDataWriter();
    }

    @Bean
    public SPMonitoringIndexJobParamValidator spMonitoringIndexJobParamValidator() {
        return new SPMonitoringIndexJobParamValidator();
    }
}
