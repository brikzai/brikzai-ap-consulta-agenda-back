from django.urls import path, re_path
from . import views

urlpatterns = [
    path("health", views.health),
    re_path(r"^webhooks/agenda/(?P<financiador_id>\d{14})$", views.webhook_agenda),
    path("webhooks/agenda/processar", views.processar_webhook_agenda),
    path("jobs/varrer-completude", views.varrer_completude),
    re_path(r"^jobs/importar-ap005/(?P<financiador_id>\d{14})$", views.importar_ap005),
    path("agendas/consultas", views.criar_consulta_agenda),
    re_path(r"^agendas/consultas/(?P<consulta_id>[0-9A-Za-z]{26})$", views.obter_consulta_agenda),
    path("config/politicas-consulta", views.politicas_consulta),
]
