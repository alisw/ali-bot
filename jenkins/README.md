Jenkins now provide a very clever way of defining build pipelines in a
programmatic way. Full documentation can be found at 

<https://wiki.jenkins-ci.org/display/JENKINS/Pipeline+Plugin>

In order to use a Pipeline you need to create a pileline job. We
highly reccomend you store your pipeline in `ali-bot/jenkins` and you
pick it up from there rather than using the inline editor. This makes
collaborating on a pipeline much easier. Since your pipelines will be
public use enough care to avoid exposing private details of the cluster
and of course to hardcode passwords. If you think you need to have a
private repository, simply come and discuss your issue and we will show
you how to separate sensible information from the rest.

The standard structure of a pipeline is:

      node ('some slave label') {

        stage 'Some serial stage'

        withCredentials([[$class: 'StringBinding',
                          credentialsId: 'some-credential-token',
                          variable: 'VAULT_TOKEN']]) {
          withEnv(["VAULT_ADDR=${VAULT_ADDR}",
                   "VAULT_SKIP_VERIFY=1"]) {
            sh """
              some shell script
            """
        }
        
        stage 'A parallel stage'
        parallel {
          stream_1: { ... }
          stream_2: { ... }
        }
      }

Where:

- `some slave label` is a jenkins label associated to one of the mesos
  worker queues. At the moment they can be:

    - slc5_x86-64-light
    - slc5_x86-64-large
    - slc5_x86-64-huge
    - slc6_x86-64-light
    - slc6_x86-64-large
    - huge_x86-64-large
    - osx_x86-64-large
    - jekyll

- Parameters specified in the jenkins GUI can be interpolated by using
  `${PARAMETER_NAME}`.

- Environment variables specified in a job can be interpolated by using
  `${env.ENVIRONMENT_VARIABLE_NAME}`

- Secrets should stay in Vault (which is fully encrypted) and should be 
  retrieved only when needed. The token used to access the vault should
  be stored using the Jenkins Credentials plugin and can be retrieved to
  an environment variable via the `withCredentials` function.

- Useful information about how to parallelize jobs on multiple machines can be
  found at
  <https://www.cloudbees.com/blog/parallelism-and-distributed-builds-jenkins>.

